"""Owned rows for ./smoke-test-hermes-egress.sh in the disposable project only.

prepare      an organization, a user, a conversation with an active turn and its
             execution, both Hermes capabilities, image generation with its
             model, a model grant and an open Hermes session bound to the turn;
             prints the session's token (no other secret)
mode M       set the organization's egress mode (none, packages or public)
web-access   grant (on) or revoke (off) web.access
end-turn     unbind the session from its turn
bind         bind it to the turn again
close        close the session
skill        an MCP skill on the reference server (get-sum), granted to the
             user; prints its id

State lives in a private journal; nothing outside the fixture organization is
touched. Run with the backend's environment (uv run in business-backend).
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg

DSN = "postgresql://postgres:postgres@127.0.0.1:55436/aurum_ai"
STATE = Path("/tmp/hermes-egress-acceptance/fixture.json")
SECRET = "hermes-egress-acceptance-only-signing-key"
MODEL = "hermes-egress-test"
IMAGE_MODEL = "hermes-egress-image"


def _save(state: dict) -> None:
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
        json.dump(state, out)
    os.replace(temporary, STATE)


async def prepare(db: asyncpg.Connection) -> None:
    if STATE.exists():
        raise SystemExit("fixture journal exists; inspect it before preparing again")
    org, user, conversation, turn, execution, session = (uuid4() for _ in range(6))
    _save({"org": str(org), "user": str(user), "conversation": str(conversation), "session": str(session)})
    now = datetime.now(timezone.utc)
    async with db.transaction():
        await db.execute("INSERT INTO organizations(id,name,slug) VALUES($1,'Hermes egress',$2)", org, f"hermes-egress-{org}")
        await db.execute(
            "INSERT INTO users(id,org_id,keycloak_id,email,role) VALUES($1,$2,$3,$4,'member')",
            user, org, uuid4(), f"{user}@hermes-egress.invalid",
        )
        for capability in ("chat.send", "agents.hermes", "images.generate"):
            await db.execute("INSERT INTO organization_capabilities(org_id,capability) VALUES($1,$2)", org, capability)
        await db.execute(
            "INSERT INTO access_grants(id,org_id,resource_type,resource_id,principal_type,principal_id,permission,created_via) "
            "VALUES($1,$2,'model',$3,'organization',$2,'read','provisioning')",
            uuid4(), org, MODEL,
        )
        await db.execute(
            "INSERT INTO organization_settings(org_id,image_generation_model) VALUES($1,$2)", org, IMAGE_MODEL
        )
        await db.execute("SELECT platform_ensure_partition('conversations',$1)", now)
        await db.execute(
            "INSERT INTO conversations(id,org_id,user_id,model,title,status) VALUES($1,$2,$3,$4,'Hermes egress','active')",
            conversation, org, user, MODEL,
        )
        await db.execute(
            "INSERT INTO interaction_turns(id,org_id,conversation_id,owner_id,limits) VALUES($1,$2,$3,$4,'{}')",
            turn, org, conversation, user,
        )
        await db.execute(
            "INSERT INTO interaction_executions(id,org_id,turn_id,workflow_id,run_id,task_queue,phase) "
            "VALUES($1,$2,$3,'hermes-acceptance',$4,'tools','ordinary')",
            execution, org, turn, uuid4(),
        )
        await db.execute(
            "INSERT INTO hermes_sessions(id,org_id,user_id,conversation_id,active_turn_id,active_execution_id,expires_at) "
            "VALUES($1,$2,$3,$4,$5,$6,$7)",
            session, org, user, conversation, turn, execution, now + timedelta(hours=1),
        )
    # The fixture's own settings: the backend's signing key, nothing else real.
    os.environ.update(APP_SECRET_KEY=SECRET, APP_DEBUG="true")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "business-backend"))
    from src.services.hermes_session_token import hermes_session_token_service

    issued = hermes_session_token_service.issue(
        org_id=org, user_id=user, conversation_id=conversation, generation=1,
        session_id=session, token_version=1, expires_at=now + timedelta(hours=1),
    )
    print(issued.token)


async def main(argv: list[str]) -> None:
    db = await asyncpg.connect(DSN)
    try:
        if argv == ["prepare"]:
            await prepare(db)
            return
        state = json.loads(STATE.read_text())
        org = UUID(state["org"])
        if len(argv) == 2 and argv[0] == "mode" and argv[1] in ("none", "packages", "public"):
            await db.execute(
                "INSERT INTO organization_settings(org_id,hermes_egress_mode) VALUES($1,$2) "
                "ON CONFLICT (org_id) DO UPDATE SET hermes_egress_mode=EXCLUDED.hermes_egress_mode",
                org, argv[1],
            )
        elif argv == ["web-access", "on"]:
            await db.execute(
                "INSERT INTO organization_capabilities(org_id,capability) VALUES($1,'web.access') ON CONFLICT DO NOTHING", org
            )
        elif argv == ["web-access", "off"]:
            await db.execute("DELETE FROM organization_capabilities WHERE org_id=$1 AND capability='web.access'", org)
        elif argv == ["end-turn"]:
            await db.execute(
                "UPDATE hermes_sessions SET active_turn_id=NULL,active_execution_id=NULL WHERE id=$1", UUID(state["session"])
            )
        elif argv == ["bind"]:
            await db.execute(
                "UPDATE hermes_sessions s SET active_turn_id=t.id,active_execution_id=e.id "
                "FROM interaction_turns t JOIN interaction_executions e ON e.turn_id=t.id "
                "WHERE s.id=$1 AND t.conversation_id=s.conversation_id AND t.status='active'",
                UUID(state["session"]),
            )
        elif argv == ["skill"]:
            skill = uuid4()
            await db.execute(
                "INSERT INTO skills(id,org_id,name,description,type,mcp_transport,mcp_endpoint,activity_name,"
                "input_schema,created_at,updated_at) VALUES($1,$2,'hermes-sum','Returns the sum of two numbers',"
                "'mcp','sse','http://mcp-everything:3001/sse','get-sum',$3,now(),now())",
                skill, org,
                json.dumps({"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}),
            )
            await db.execute(
                "INSERT INTO access_grants(id,org_id,resource_type,resource_id,principal_type,principal_id,permission,created_via) "
                "VALUES($1,$2,'skill',$3,'user',$4,'read','provisioning')",
                uuid4(), org, str(skill), UUID(state["user"]),
            )
            print(skill)
        elif argv == ["close"]:
            await db.execute(
                "UPDATE hermes_sessions SET state='closed',closed_at=now(),active_turn_id=NULL,active_execution_id=NULL,"
                "token_version=token_version+1 WHERE id=$1",
                UUID(state["session"]),
            )
        else:
            raise SystemExit("usage: prepare | mode M | web-access on|off | end-turn | bind | close | skill")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
