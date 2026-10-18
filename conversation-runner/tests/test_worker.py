from unittest.mock import AsyncMock, patch

from src import worker
from src.workflows.activities import get_conversation_temperature


async def test_production_worker_registers_conversation_temperature_activity():
    # Intercept the SDK boundary, keeping the production registration path real.
    with patch.object(worker, "Worker") as sdk_worker:
        sdk_worker.return_value.run = AsyncMock()
        await worker.run(object())

    assert get_conversation_temperature in sdk_worker.call_args.kwargs["activities"]


async def test_production_worker_registers_evaluator_configuration_activity():
    from src.workflows import activities
    with patch.object(worker, "Worker") as sdk_worker:
        sdk_worker.return_value.run = AsyncMock()
        await worker.run(object())
    assert hasattr(activities, "get_evaluator_config")
    assert activities.get_evaluator_config in sdk_worker.call_args.kwargs["activities"]
