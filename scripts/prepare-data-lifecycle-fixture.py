"""Create exact old-history test rows only in the fixed disposable PostgreSQL project."""

import asyncio
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg

STATE = Path("/tmp/phaseb-lifecycle-acceptance/archive-fixture.json")
TITLE = "Lifecycle archive acceptance — 26 historical turns"


def save_prepared(state):
    state["prepared"] = True
    temporary = STATE.with_suffix(".prepared-tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        json.dump(state, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, STATE)


def expected_messages(state):
    created = datetime.fromisoformat(state["created_at"])
    for index, turn in enumerate(state["turns"]):
        for offset, role in enumerate(("user", "assistant"), 1):
            at = created + timedelta(seconds=index * 2 + offset)
            yield {
                "id": UUID(turn[role + "_message"]), "org_id": UUID(state["org_id"]),
                "conversation_id": UUID(state["conversation_id"]), "turn_id": UUID(turn["id"]),
                "role": role, "content": f"Archive fixture {'question' if role == 'user' else 'answer'} {index + 1:02d}",
                "created_at": at, "updated_at": at,
                "user_id": UUID(state["user_id"]) if role == "user" else None,
                "tokens_in": 0 if role == "user" else 1,
                "tokens_out": 0 if role == "user" else 1,
                "cost_usd": Decimal("0") if role == "user" else Decimal("0.000001"),
                "model": "lifecycle-test",
            }


def verify_messages(state, rows):
    expected = list(expected_messages(state))
    if len(expected) != 52 or [dict(row) for row in rows] != expected:
        raise ValueError("established_fixture_content_mismatch")


def owned_postgres() -> str:
    container = json.loads(subprocess.check_output(
        ["docker", "inspect", "phaseb-lifecycle-test-postgres-1"], text=True
    ))[0]
    labels = container["Config"]["Labels"]
    if (labels.get("com.docker.compose.project"), labels.get("com.docker.compose.service")) != (
        "phaseb-lifecycle-test", "postgres"
    ):
        raise ValueError("ownership_mismatch")
    if container.get("State", {}).get("Running") is not True:
        raise ValueError("owned_database_not_running")
    if container["NetworkSettings"]["Ports"]["5432/tcp"] != [
        {"HostIp": "127.0.0.1", "HostPort": "55434"}
    ]:
        raise ValueError("unexpected_database_binding")
    return container["Id"]


async def prepare() -> None:
    container_id = owned_postgres()
    connection = await asyncpg.connect("postgresql://postgres:postgres@127.0.0.1:55434/aurum_ai")
    try:
        # This column must exist before allocation; new rows acquire complete provenance.
        await connection.fetchval("SELECT execution_provenance_complete FROM conversation_identities LIMIT 1")
        owners = await connection.fetch("SELECT id,org_id FROM users WHERE email='user@example.com'")
        if len(owners) != 1:
            raise ValueError("owned_login_required")
        owner = owners[0]
        if STATE.exists():
            state = json.loads(STATE.read_text())
            if (state["postgres_container_id"], state["user_id"], state["org_id"]) != (
                container_id, str(owner["id"]), str(owner["org_id"])
            ):
                raise ValueError("established_fixture_mismatch")
        else:
            state = {
                "version": 1, "postgres_container_id": container_id,
                "user_id": str(owner["id"]), "org_id": str(owner["org_id"]),
                "conversation_id": str(uuid4()), "operation_key": str(uuid4()),
                "created_at": (datetime.now(timezone.utc) - timedelta(days=181)).isoformat(),
                "turns": [{"id": str(uuid4()), "user_message": str(uuid4()),
                           "assistant_message": str(uuid4())} for _ in range(26)],
            }
            STATE.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
                json.dump(state, output)
        conversation_id = UUID(state["conversation_id"])
        identity = await connection.fetchrow(
            "SELECT lifecycle,archive_id FROM conversation_identities WHERE id=$1", conversation_id
        )
        if identity is not None:
            if identity["lifecycle"] != "active" or identity["archive_id"] is not None:
                raise ValueError("fixture_already_entered_archive")
            rows = await connection.fetch("""SELECT id,org_id,conversation_id,turn_id,role,content,
                created_at,updated_at,user_id,tokens_in,tokens_out,cost_usd,model FROM messages
                WHERE conversation_id=$1 ORDER BY created_at,id""", conversation_id)
            verify_messages(state, rows)
            exists = await connection.fetchval("SELECT count(*) FROM conversations WHERE id=$1 AND org_id=$2 AND user_id=$3", conversation_id, owner["org_id"], owner["id"])
            if exists != 1:
                raise ValueError("established_fixture_incomplete")
            save_prepared(state)
            print("Owned old-history fixture already prepared")
            return
        if any(state.get(marker) for marker in ("prepared", "run_id", "runner_id", "attachment")):
            raise ValueError("established_fixture_identity_missing")
        created = datetime.fromisoformat(state["created_at"])
        last = created + timedelta(seconds=52)
        async with connection.transaction():
            await connection.execute("SET LOCAL ROLE aurum_ai_app")
            await connection.execute("SELECT set_config('app.current_org_id',$1,true)", str(owner["org_id"]))
            await connection.execute("""INSERT INTO conversations
                (id,org_id,user_id,created_at,updated_at,last_message_at,title,model,mode,status,extra,
                 token_count_in,token_count_out,cost_usd)
                VALUES ($1,$2,$3,$4,$5,$5,$6,'lifecycle-test','chat','active','{}',26,26,$7)""",
                conversation_id, owner["org_id"], owner["id"], created, last, TITLE, Decimal("0.000026"))
            for index, turn in enumerate(state["turns"]):
                for offset, role in enumerate(("user", "assistant"), 1):
                    at = created + timedelta(seconds=index * 2 + offset)
                    content = f"Archive fixture {'question' if role == 'user' else 'answer'} {index + 1:02d}"
                    await connection.execute("""INSERT INTO messages
                        (id,org_id,conversation_id,turn_id,user_id,role,content,model,created_at,updated_at,
                         tokens_in,tokens_out,cost_usd,tool_calls,tool_results,extra)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,'lifecycle-test',$8,$8,$9,$9,$10,'[]','{}','{}')""",
                        UUID(turn[role + "_message"]), owner["org_id"], conversation_id, UUID(turn["id"]),
                        owner["id"] if role == "user" else None, role, content, at,
                        0 if role == "user" else 1, Decimal("0") if role == "user" else Decimal("0.000001"))
        save_prepared(state)
        print("Prepared 26 historical turns under the real app role; private UUID journal saved")
    finally:
        await connection.close()


if __name__ == "__main__":
    try:
        asyncio.run(prepare())
    except Exception:
        raise SystemExit("Owned history preparation failed; preserve journal and inspect exact fixture") from None
