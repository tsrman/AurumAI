# ruff: noqa: F811
"""Actual HTTP admission against a committed real archive claim; SQL feedback surface."""

import asyncio
from uuid import uuid4
from unittest.mock import AsyncMock
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from src.core.config import settings
from src.core.database import _AppSession
from src.core import dependencies
from src.core.auth import UserInfo, get_current_user, get_current_user_or_session
from src.routers import chat
from src.services.quota import get_quota_service
from tests.routers.test_chat_completions import RecordingQuotaService
from tests.services.test_business_archive_service import archive_fixture, LocalArchiveStorage  # noqa:F401
from src.services.archive.service import ArchiveService


@pytest.mark.parametrize("surface", ["send", "score", "persist", "feedback"])
async def test_committed_real_claim_fences_existing_write_surface(
    archive_fixture, tmp_path, monkeypatch, surface
):
    sessions, org, conversation = archive_fixture
    feedback_id = uuid4()
    async with sessions() as session:
        await session.execute(
            text("UPDATE conversations SET status='active' WHERE id=:c"), dict(c=conversation)
        )
        user_id = await session.scalar(
            text("SELECT user_id FROM conversations WHERE id=:c"), dict(c=conversation)
        )
        if surface == "feedback":
            created=await session.scalar(text("SELECT created_at FROM conversations WHERE id=:c"),dict(c=conversation))
            await session.execute(text("SELECT platform_ensure_partition('messages',:at)"),dict(at=created))
            await session.execute(
                text(
                    "INSERT INTO messages(id,org_id,conversation_id,role,content,created_at,updated_at) SELECT :m,org_id,id,'assistant','original',created_at,created_at FROM conversations WHERE id=:c"
                ),
                dict(m=feedback_id, c=conversation),
            )
            await session.execute(
                text(
                    "INSERT INTO conversation_executions(id,org_id,conversation_id,generation,namespace,workflow_id,run_id,workflow_type,task_queue,terminal_status,closed_at,observed_at,tree_complete) VALUES(:id,:o,:c,1,'default',:wf,:r,'ConversationWorkflow','test','COMPLETED',now(),now(),true)"
                ),
                dict(
                    id=uuid4(), o=org, c=conversation, wf=f"archive-race-{conversation}", r=uuid4()
                ),
            )
        await session.commit()
    engine = create_async_engine(
        settings.DATABASE_URL,
        hide_parameters=True,
        connect_args={"server_settings": {"role": "aurum_ai_app"}},
    )
    app_sessions = async_sessionmaker(
        engine, sync_session_class=_AppSession, expire_on_commit=False
    )
    monkeypatch.setattr(dependencies, "async_session", app_sessions)
    user = UserInfo(
        user_id=str(user_id),
        org_id=str(org),
        role="member",
        raw_claims={"type": "session", "conversation_id": str(conversation), "generation": 1},
    )
    app = FastAPI()
    app.include_router(chat.router, prefix="/v1")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_user_or_session] = lambda: user
    app.dependency_overrides[get_quota_service] = RecordingQuotaService
    monkeypatch.setattr(chat.authorization_service, "has_capability", AsyncMock(return_value=True))
    service = ArchiveService(sessions, LocalArchiveStorage(tmp_path / "objects"))
    entered, release = asyncio.Event(), asyncio.Event()
    original = service._seal

    async def pause(*args):
        entered.set()
        await release.wait()
        await original(*args)

    monkeypatch.setattr(service, "_seal", pause)
    task = asyncio.create_task(service.run(org, conversation, apply=True))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if surface == "feedback":
            # No feedback HTTP endpoint exists. Exercise the real direct SQL fence
            # on the feedback column, including the parent's committed claim.
            from sqlalchemy.exc import DBAPIError

            async with app_sessions() as session:
                session.info["org_id"] = str(org)
                with pytest.raises(DBAPIError):
                    await session.execute(
                        text("UPDATE messages SET feedback='good' WHERE id=:id AND org_id=:o"),
                        dict(id=feedback_id, o=org),
                    )
        else:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                if surface == "send":
                    response = await client.post(
                        "/v1/chat/stream",
                        json={
                            "conversation_id": str(conversation),
                            "model": "test",
                            "messages": [{"role": "user", "content": "late"}],
                        },
                    )
                elif surface == "score":
                    response = await client.patch(
                        f"/v1/conversations/{conversation}/messages/{uuid4()}/score",
                        json={"score": 0.7, "reason": "late"},
                    )
                else:
                    response = await client.post(
                        f"/v1/conversations/{conversation}/messages",
                        json={"turn_id": str(uuid4()), "role": "assistant", "content": "late"},
                    )
                assert response.status_code == 409
        release.set()
        await asyncio.wait_for(task, 10)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()


@pytest.mark.parametrize("surface", ["send", "score", "persist", "feedback"])
async def test_writer_first_holds_parent_until_commit_before_real_claim(
    archive_fixture, tmp_path, monkeypatch, surface
):
    from contextlib import asynccontextmanager
    from src.services.archive.schemas import read_rows
    from src.services.runner_runtime import RunnerRuntimeError

    sessions, org, conversation = archive_fixture
    turn, message = uuid4(), uuid4()
    async with sessions() as db:
        user_id = await db.scalar(
            text("SELECT user_id FROM conversations WHERE id=:c"), dict(c=conversation)
        )
        await db.execute(
            text("UPDATE conversations SET status='active' WHERE id=:c"), dict(c=conversation)
        )
        created=await db.scalar(text("SELECT created_at FROM conversations WHERE id=:c"),dict(c=conversation))
        await db.execute(text("SELECT platform_ensure_partition('messages',:at)"),dict(at=created))
        await db.execute(
            text(
                "INSERT INTO messages(id,org_id,conversation_id,turn_id,role,content,created_at,updated_at) SELECT :m,org_id,id,:t,'assistant','original',created_at,created_at FROM conversations WHERE id=:c"
            ),
            dict(m=message, t=turn, c=conversation),
        )
        await db.execute(
            text(
                "INSERT INTO conversation_executions(id,org_id,conversation_id,generation,namespace,workflow_id,run_id,workflow_type,task_queue,terminal_status,closed_at,observed_at,tree_complete) VALUES(:id,:o,:c,1,'default',:wf,:r,'ConversationWorkflow','test','COMPLETED',now(),now(),true)"
            ),
            dict(id=uuid4(), o=org, c=conversation, wf=f"archive-writer-{conversation}", r=uuid4()),
        )
        await db.commit()
    engine = create_async_engine(
        settings.DATABASE_URL,
        hide_parameters=True,
        connect_args={"server_settings": {"role": "aurum_ai_app"}},
    )
    app_sessions = async_sessionmaker(
        engine, sync_session_class=_AppSession, expire_on_commit=False
    )
    monkeypatch.setattr(dependencies, "async_session", app_sessions)
    user = UserInfo(
        user_id=str(user_id),
        org_id=str(org),
        role="member",
        raw_claims={"type": "session", "conversation_id": str(conversation), "generation": 1},
    )
    app = FastAPI()
    app.include_router(chat.router, prefix="/v1")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_current_user_or_session] = lambda: user
    app.dependency_overrides[get_quota_service] = lambda: AsyncMock()
    monkeypatch.setattr(chat.authorization_service, "has_capability", AsyncMock(return_value=True))
    monkeypatch.setattr(chat, "ensure_model_allowed", AsyncMock())
    monkeypatch.setattr(chat, "_maybe_classify_workflow", AsyncMock(return_value=None))
    monkeypatch.setattr(chat, "get_langfuse", lambda: None)
    monkeypatch.setattr(chat.event_bus, "publish", AsyncMock())
    monkeypatch.setattr(chat.event_bus, "subscribe_channel", AsyncMock(return_value=AsyncMock()))
    monkeypatch.setattr(
        chat.session_manager,
        "get_or_create",
        AsyncMock(side_effect=RunnerRuntimeError("fixture unavailable after committed send")),
    )
    entered, release, claim_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    writer_pid = None
    claim_pid = None

    async def held(session):
        nonlocal writer_pid
        writer_pid = await session.scalar(text("SELECT pg_backend_pid()"))
        entered.set()
        await release.wait()

    if surface == "send":
        original = chat._get_owned_conversation

        async def pause(session, *args, **kwargs):
            result = await original(session, *args, **kwargs)
            await held(session)
            return result

        monkeypatch.setattr(chat, "_get_owned_conversation", pause)
    elif surface in ("score", "persist"):
        method = "update_score_by_turn" if surface == "score" else "upsert_assistant_by_turn"
        original = getattr(chat.message_repository, method)

        async def pause(session, *args, **kwargs):
            await held(session)
            return await original(session, *args, **kwargs)

        monkeypatch.setattr(chat.message_repository, method, pause)

    class Service(ArchiveService):
        @asynccontextmanager
        async def session(self, tenant):
            nonlocal claim_pid
            async with super().session(tenant) as session:
                if claim_pid is None:
                    claim_pid = await session.scalar(text("SELECT pg_backend_pid()"))
                    claim_started.set()
                yield session

    service = Service(sessions, LocalArchiveStorage(tmp_path / "objects"))
    tasks = []
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:

            async def write():
                if surface == "feedback":
                    async with app_sessions() as db:
                        db.info["org_id"] = str(org)
                        await db.execute(
                            text("SELECT platform_lock_live_conversation(:o,:c)"),
                            dict(o=org, c=conversation),
                        )
                        await held(db)
                        await db.execute(
                            text("UPDATE messages SET feedback='good' WHERE id=:m"), dict(m=message)
                        )
                        await db.commit()
                    return None
                if surface == "send":
                    return await client.post(
                        "/v1/chat/stream",
                        json={
                            "conversation_id": str(conversation),
                            "model": "test",
                            "messages": [{"role": "user", "content": "writer-first"}],
                        },
                    )
                if surface == "score":
                    return await client.patch(
                        f"/v1/conversations/{conversation}/messages/{turn}/score",
                        json={"score": 0.7, "reason": "writer-first"},
                    )
                return await client.post(
                    f"/v1/conversations/{conversation}/messages",
                    json={"turn_id": str(turn), "role": "assistant", "content": "writer-first"},
                )

            writing = asyncio.create_task(write())
            tasks.append(writing)
            await asyncio.wait_for(entered.wait(), 5)
            claiming = asyncio.create_task(service.run(org, conversation, apply=True))
            tasks.append(claiming)
            await asyncio.wait_for(claim_started.wait(), 5)
            # Assert PostgreSQL's actual lock graph, not only a timing guess.
            async with sessions() as probe:
                for _ in range(100):
                    blockers = await probe.scalar(
                        text("SELECT pg_blocking_pids(:pid)"), dict(pid=claim_pid)
                    )
                    if writer_pid in blockers:
                        break
                    await asyncio.sleep(0.01)
                assert writer_pid in blockers
            release.set()
            response = await asyncio.wait_for(writing, 10)
            result = await asyncio.wait_for(claiming, 10)
            if surface in ("send", "persist"):
                assert response.status_code == (503 if surface == "send" else 201)
                assert result["eligible"] is False and result["blocked_reason"] in (
                    "recent_activity",
                    "active_turn",
                    "unsettled_operation",
                )
                async with sessions() as db:
                    assert (
                        await db.scalar(
                            text(
                                "SELECT count(*) FROM messages WHERE conversation_id=:c AND content='writer-first'"
                            ),
                            dict(c=conversation),
                        )
                        == 1
                    )
            else:
                assert response is None or response.status_code in (200, 409)
                output = tmp_path / "export"
                await service.export(org, result["archive_id"], output)
                import json

                manifest = json.loads((output / "manifest.json").read_text())
                rows = []
                for index, part in enumerate(manifest["parts"]):
                    if part["table"] == "messages":
                        rows.extend(read_rows("messages", output / f"{index:06d}.parquet"))
                assert len(rows) == 1
                if surface == "score":
                    assert rows[0]["extra"]["faithfulness_score"] == 0.7
                else:
                    assert rows[0]["feedback"] == "good"
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()
