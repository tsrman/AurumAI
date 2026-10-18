import importlib.util


def test_data_worker_is_implemented():
    assert (
        importlib.util.find_spec("src.workflows.data_lifecycle") is not None
    ), "data workflow missing"
    from src.workflows.data_lifecycle import DataLifecycleInput, DATA_LIFECYCLE_QUEUE
    from src.maintenance_worker import data_healthy
    from dataclasses import fields

    assert {field.name for field in fields(DataLifecycleInput)} == {
        "org_id",
        "conversation_id",
        "archive_id",
        "audit",
    }
    assert DATA_LIFECYCLE_QUEUE == "platform-data-lifecycle"
    assert isinstance(data_healthy(), bool)


async def test_observer_loop_is_independent_of_storage_and_disabled_flag(monkeypatch):
    import asyncio
    from src import maintenance_worker as worker
    from src.core.config import settings
    from src.services import execution_closure
    from unittest.mock import AsyncMock

    assert hasattr(worker, "_data_observer_loop"), "observer needs independent loop"
    monkeypatch.setattr(settings, "ARCHIVE_ENABLED", False)
    observed = asyncio.Event()

    async def observe(*args):
        observed.set()

    monkeypatch.setattr(execution_closure, "observe_pending_executions", observe)
    task = asyncio.create_task(worker._data_observer_loop(AsyncMock()))
    try:
        await asyncio.wait_for(observed.wait(), 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
