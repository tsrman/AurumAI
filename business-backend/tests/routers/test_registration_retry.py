"""Real runner activity, HTTP API, PostgreSQL and exact Temporal histories."""

# ruff: noqa: F811
import asyncio
import os
import re
import socket
import sys
from pathlib import Path
from uuid import UUID

import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from temporalio.client import WorkflowFailureError
from temporalio.service import RPCError, RPCStatusCode

from src.core import dependencies
from src.core.auth import UserInfo, get_current_user_or_session
from src.core.config import settings
from src.core.database import _AppSession
from src.models.archive_control import ConversationExecution
from src.routers import chat
from tests.services.test_execution_closure import receipt_client  # noqa: F401
from tests.services.test_interaction_outbox import outbox_fixture  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unavailable", "sibling_progress", "generation_mismatch"])
async def test_real_runner_registration_retries_observation_but_not_identity_violation(
    outbox_fixture, receipt_client, monkeypatch, fault
):
    fixture, client = outbox_fixture, receipt_client
    engine = create_async_engine(
        settings.DATABASE_URL,
        poolclass=NullPool,
        connect_args={"server_settings": {"role": "aurum_ai_app"}},
    )
    sessions = async_sessionmaker(engine, sync_session_class=_AppSession, expire_on_commit=False)
    monkeypatch.setattr(dependencies, "async_session", sessions)
    user = UserInfo(
        user_id=str(fixture.user_id),
        org_id=str(fixture.org_id),
        role="member",
        raw_claims={
            "type": "session",
            "conversation_id": str(fixture.conversation_id),
            "generation": 1,
        },
    )
    app = FastAPI()
    app.include_router(chat.router, prefix="/v1")
    app.dependency_overrides[get_current_user_or_session] = lambda: user
    work_started = asyncio.Event()
    fault_seen = asyncio.Event()
    observations = 0
    statuses = []

    @app.post("/test/work-started")
    async def work():
        async with fixture.sessions() as observer:
            assert (
                await observer.scalar(
                    select(ConversationExecution.id).where(
                        ConversationExecution.conversation_id == fixture.conversation_id
                    )
                )
                is not None
            )
        work_started.set()
        return {}

    @app.middleware("http")
    async def record_status(request, call_next):
        response = await call_next(request)
        if request.url.path.endswith("/executions"):
            statuses.append(response.status_code)
        return response

    class ObservedHandle:
        def __init__(self, handle):
            self.handle = handle

        async def describe(self, **kwargs):
            if fault == "unavailable" and not fault_seen.is_set():
                fault_seen.set()
                raise RPCError("fixture unavailable", RPCStatusCode.UNAVAILABLE, b"")
            return await self.handle.describe(**kwargs)

        async def fetch_history_events(self, **kwargs):
            nonlocal observations
            observations += 1
            # Return an earlier, real prefix of this root's exact history on
            # the first tree inspection, between sibling initiation and start.
            partial = fault == "sibling_progress" and observations == 2
            async for event in self.handle.fetch_history_events(**kwargs):
                yield event
                if partial and event.HasField(
                    "start_child_workflow_execution_initiated_event_attributes"
                ):
                    fault_seen.set()
                    return

    class ObservationClient:
        namespace = client.namespace
        data_converter = client.data_converter

        def get_workflow_handle(self, *args, **kwargs):
            return ObservedHandle(client.get_workflow_handle(*args, **kwargs))

    monkeypatch.setattr(chat, "get_temporal_client", lambda: ObservationClient())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    worker = None
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        runner = Path(__file__).resolve().parents[3] / "conversation-runner"
        worker = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.lifecycle_registration_worker",
            cwd=str(runner),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                "PATH": os.environ["PATH"],
                "LANGFUSE_ENABLED": "false",
                "SESSION_TOKEN": "isolated-test-session",
                "CONVERSATION_ID": str(fixture.conversation_id),
                "LLM_GATEWAY_URL": f"http://127.0.0.1:{port}",
                "REGISTRATION_TEMPORAL_HOST": client.service_client.config.target_host,
            },
        )
        ready = await asyncio.wait_for(worker.stdout.readline(), 15)
        if ready != b"READY\n":
            diagnostic = await worker.stderr.read()
            # Report only the exception type; never raw settings/HTTP errors.
            error_types = re.findall(r"^([\w.]*Error):", diagnostic.decode(), re.MULTILINE)
            frames = re.findall(r'File "([^"\n]+)", line (\d+), in (\w+)', diagnostic.decode())
            pytest.fail(f"Fixture worker startup failed ({error_types}, {frames})")
        handle = await client.start_workflow(
            "ConversationWorkflow",
            {
                "conversation_id": str(fixture.conversation_id),
                "lifecycle_generation": 2 if fault == "generation_mismatch" else 1,
                "sibling": fault == "sibling_progress",
            },
            id=f"conversation-{fixture.conversation_id}",
            task_queue=f"session-{fixture.conversation_id}",
        )
        if fault == "generation_mismatch":
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), 15)
            assert statuses == [409] and not work_started.is_set()
        else:
            await asyncio.wait_for(fault_seen.wait(), 10)
            assert not work_started.is_set()
            await asyncio.wait_for(handle.result(), 20)
            assert statuses[0] == 503 and statuses[-1] == 200
            assert work_started.is_set()
        assert UUID(handle.result_run_id)
    finally:
        if worker is not None and worker.returncode is None:
            worker.stdin.write(b"\n")
            await worker.stdin.drain()
            await asyncio.wait_for(worker.wait(), 15)
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()
        await engine.dispose()
        async with fixture.sessions.begin() as cleanup:
            await cleanup.execute(
                delete(ConversationExecution).where(
                    ConversationExecution.conversation_id == fixture.conversation_id
                )
            )
