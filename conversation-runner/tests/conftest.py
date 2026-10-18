import pytest
from temporalio import activity
from temporalio.worker import Worker
import os
import sys
from pathlib import Path

# Make the `src` package importable from the test directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# conversation_id/user_id/session_token are optional in src.config (pool-mode
# containers boot without them, see src/health.py's /bind), but most tests
# assume a bound, direct-mode runner — default them so every test doesn't
# have to set this boilerplate up itself.
os.environ.setdefault("CONVERSATION_ID", "test-conversation")
os.environ.setdefault("USER_ID", "test-user")
os.environ.setdefault("SESSION_TOKEN", "test-token")
os.environ.setdefault("LANGFUSE_ENABLED", "false")

# Workflow suites replace backend activities with in-process fakes. Register the
# new mandatory backend identity boundary alongside those fakes; dedicated tests
# replace this default to exercise retries and the before-work barrier.


@activity.defn(name='register_conversation_execution')
async def registered_execution(workflow_id: str, run_id: str) -> None:
    assert workflow_id and run_id


@pytest.fixture(autouse=True)
def lifecycle_backend_boundary(monkeypatch):
    original = Worker.__init__
    def initialize(self, *args, **kwargs):
        activities = list(kwargs.get('activities', []))
        if kwargs.get('workflows') and not any(activity._Definition.from_callable(fn).name == 'register_conversation_execution' for fn in activities):
            kwargs['activities'] = [*activities, registered_execution]
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Worker, '__init__', initialize)
