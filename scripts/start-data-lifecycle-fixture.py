"""Start one exact disposable idle workflow; never replace an uncertain start."""

import json
import os
from pathlib import Path
import subprocess
from uuid import UUID

STATE = Path("/tmp/phaseb-lifecycle-acceptance/archive-fixture.json")
PROJECT = "phaseb-lifecycle-test"
NETWORK = PROJECT + "_runner"

# Executed inside the disposable API container, so production runtime dependencies
# use that container's isolated configuration. No host dotenv or credential output.
START = """
import asyncio, json, sys
from uuid import UUID
from sqlalchemy import text
from src.core.cache import redis_client
from src.core.config import settings
from src.core.database import async_session, engine
from src.core.temporal import connect_temporal, disconnect_temporal, get_temporal_client
from src.services.execution_closure import start_idle_conversation
from src.services.session_manager import session_manager

async def main():
    if settings.DOCKER_NETWORK != 'phaseb-lifecycle-test_runner' or settings.TEMPORAL_HOST != 'temporal:7233':
        raise RuntimeError('isolated_runtime_required')
    org, conversation, operation = map(UUID, sys.argv[1:])
    await redis_client.connect()
    await connect_temporal()
    try:
        async with async_session() as session:
            session.info['org_id'] = str(org)
            if await session.scalar(text('SELECT current_user')) != 'aurum_ai_app':
                raise RuntimeError('ordinary_app_role_required')
            run_id = await start_idle_conversation(session, get_temporal_client(), org_id=org,
                conversation_id=conversation, operation_key=operation)
            print(json.dumps({'run_id': str(run_id)}))
    finally:
        await session_manager.close()
        await disconnect_temporal()
        await redis_client.disconnect()
        await engine.dispose()
try:
    asyncio.run(main())
except Exception:
    raise SystemExit('Exact idle start unresolved; preserve journal and durable operation') from None
"""


def inspect(name):
    result = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)[0]


def main():
    state = json.loads(STATE.read_text())
    postgres = inspect(PROJECT + "-postgres-1")
    backend = inspect(PROJECT + "-backend-1")
    for service, container in (("postgres", postgres), ("backend", backend)):
        labels = container["Config"]["Labels"]
        if labels.get("com.docker.compose.project") != PROJECT or labels.get("com.docker.compose.service") != service:
            raise ValueError("ownership_mismatch")
    if postgres["Id"] != state["postgres_container_id"] or NETWORK not in backend["NetworkSettings"]["Networks"]:
        raise ValueError("established_fixture_mismatch")
    args = [str(UUID(state[key])) for key in ("org_id", "conversation_id", "operation_key")]
    if "run_id" in state:
        UUID(state["run_id"])
        print("Established exact idle run is already journalled; observe its original identity")
        return
    result = subprocess.run(["docker", "exec", "-i", backend["Id"], "python", "-", *args],
                            input=START, text=True, capture_output=True, check=True)
    receipt = json.loads(result.stdout.splitlines()[-1])
    run_id = str(UUID(receipt["run_id"]))
    runner = inspect("runner-" + args[1])
    if set(runner["NetworkSettings"]["Networks"]) != {NETWORK}:
        raise ValueError("runner_network_mismatch")
    if "CONVERSATION_ID=" + args[1] not in runner["Config"]["Env"]:
        raise ValueError("runner_binding_mismatch")
    state.update(run_id=run_id, workflow_id="conversation-" + args[1], runner_id=runner["Id"])
    temporary = STATE.with_suffix(".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        json.dump(state, output)
    os.replace(temporary, STATE)
    print("Exact idle workflow and runner identities saved; wait for natural 600-second closure")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Owned idle fixture start failed; preserve journal and reconcile its exact operation") from None
