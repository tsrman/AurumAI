import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from src import main_business


@pytest.mark.asyncio
async def test_lifespan_skips_kubernetes_pool_and_closes_runtime_after_tasks(monkeypatch) -> None:
    events: list[str] = []

    async def noop() -> None:
        return None

    async def cleanup_stale() -> int:
        return 0

    async def neverending(name: str) -> None:
        events.append(f"{name}:started")
        try:
            await asyncio.Event().wait()
        finally:
            events.append(f"{name}:cancelled")

    async def reaper() -> None:
        await neverending("reaper")

    async def pool() -> None:
        await neverending("pool")

    async def workflow_reaper() -> None:
        await neverending("workflow-reaper")

    async def outbox_dispatcher() -> None:
        await neverending("outbox-dispatcher")

    async def interaction_maintenance(runtime) -> None:  # noqa: ANN001
        assert runtime is main_business.session_manager
        await neverending("interaction-maintenance")

    async def web_result_scrubber() -> None:
        await neverending("web-result-scrubber")

    async def close() -> None:
        events.append("runtime:closed")

    monkeypatch.setattr(main_business.settings, "RUNNER_RUNTIME", "kubernetes")
    monkeypatch.setattr(main_business.settings, "INIT_DB_ON_STARTUP", False)
    monkeypatch.setattr(main_business.redis_client, "connect", noop)
    monkeypatch.setattr(main_business.redis_client, "disconnect", noop)
    monkeypatch.setattr(main_business.keycloak_client, "load_jwks", noop)
    monkeypatch.setattr(main_business, "connect_temporal", noop)
    monkeypatch.setattr(main_business, "disconnect_temporal", noop)
    monkeypatch.setattr(main_business, "engine", SimpleNamespace(dispose=noop))
    monkeypatch.setattr(main_business.session_manager, "cleanup_stale", cleanup_stale)
    monkeypatch.setattr(main_business.session_manager, "sweep_orphans", noop)
    monkeypatch.setattr(main_business.session_manager, "run_reaper", reaper)
    monkeypatch.setattr(main_business.session_manager, "maintain_pool", pool)
    monkeypatch.setattr(main_business.session_manager, "close", close)
    monkeypatch.setattr(
        main_business.workflow_run_service,
        "run_retention_reaper",
        workflow_reaper,
    )
    monkeypatch.setattr(
        main_business,
        "run_interaction_outbox_dispatcher",
        outbox_dispatcher,
    )
    monkeypatch.setattr(
        main_business,
        "run_interaction_maintenance",
        interaction_maintenance,
        raising=False,
    )
    monkeypatch.setattr(
        main_business,
        "run_web_result_scrubber",
        web_result_scrubber,
        raising=False,
    )

    async with main_business.lifespan(FastAPI()):
        await asyncio.sleep(0)

    assert events == [
        "reaper:started",
        "workflow-reaper:started",
        "outbox-dispatcher:started",
        "interaction-maintenance:started",
        "web-result-scrubber:started",
        "reaper:cancelled",
        "workflow-reaper:cancelled",
        "outbox-dispatcher:cancelled",
        "interaction-maintenance:cancelled",
        "web-result-scrubber:cancelled",
        "runtime:closed",
    ]
