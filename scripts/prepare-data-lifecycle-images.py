"""Create an old conversation with images only in the fixed disposable lifecycle project.

One finished turn with complete execution provenance, a generated image with its
tool operation, an edit of it (self-reference) and a user upload. Rows are written
under the ordinary app role; execution receipts, which only the operator observer
writes, under the owner account. Bytes go to the business bucket as the backend's
own account would put them. A private journal records every identity and digest.
"""

import asyncio
import hashlib
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import boto3

STATE = Path("/tmp/phaseb-lifecycle-acceptance/images-fixture.json")
BUCKET = "lifecycle-business"
IMAGES = {
    "generated": b"\x89PNG\r\n\x1a\nlifecycle generated image",
    "edited": b"\x89PNG\r\n\x1a\nlifecycle edited image, a little larger",
    "uploaded": b"\xff\xd8\xfflifecycle uploaded photo",
}


def history_fixture():
    spec = importlib.util.spec_from_file_location(
        "history_fixture", Path(__file__).with_name("prepare-data-lifecycle-fixture.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def business_storage():
    return boto3.client("s3", endpoint_url="http://127.0.0.1:59000", aws_access_key_id="lifecycle-business",
                        aws_secret_access_key="lifecycle-business-password", region_name="us-east-1")


def save(state) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix(".images-tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        json.dump(state, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, STATE)


def new_state(container_id: str) -> dict:
    org, user, conversation = uuid4(), uuid4(), uuid4()
    operations = {"generated": str(uuid4()), "edited": str(uuid4())}
    images = {name: str(uuid4()) for name in IMAGES}
    prefix = f"conversation-images/{org}/{conversation}"
    return {
        "version": 1, "postgres_container_id": container_id,
        "org_id": str(org), "user_id": str(user), "conversation_id": str(conversation),
        "turn_id": str(uuid4()), "execution_id": str(uuid4()), "run_id": str(uuid4()),
        "created_at": (datetime.now(timezone.utc) - timedelta(days=181)).isoformat(),
        "operations": operations, "images": images,
        "keys": {
            "generated": f"{prefix}/{operations['generated']}.png",
            "edited": f"{prefix}/{operations['edited']}.png",
            "uploaded": f"{prefix}/uploads/{images['uploaded']}.jpg",
        },
        "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in IMAGES.items()},
    }


async def prepare() -> None:
    container_id = history_fixture().owned_postgres()
    if STATE.exists():
        state = json.loads(STATE.read_text())
        if state["postgres_container_id"] != container_id:
            raise ValueError("established_fixture_mismatch")
        if state.get("prepared"):
            print("Owned image fixture already prepared")
            return
        raise ValueError("unfinished_fixture_requires_inspection")
    state = new_state(container_id)
    save(state)
    org, user, conversation = UUID(state["org_id"]), UUID(state["user_id"]), UUID(state["conversation_id"])
    turn, execution, run = UUID(state["turn_id"]), UUID(state["execution_id"]), UUID(state["run_id"])
    at = datetime.fromisoformat(state["created_at"])
    owner = await asyncpg.connect("postgresql://postgres:postgres@127.0.0.1:55434/aurum_ai")
    try:
        async with owner.transaction():
            await owner.execute("INSERT INTO organizations(id,name,slug) VALUES($1,'Lifecycle images',$2)", org, f"lifecycle-images-{org}")
            await owner.execute("INSERT INTO users(id,org_id,keycloak_id,email) VALUES($1,$2,$3,$4)", user, org, uuid4(), f"{user}@lifecycle.invalid")
            await owner.execute("SELECT platform_ensure_partition('conversations',$1)", at)
        async with owner.transaction():
            await owner.execute("SET LOCAL ROLE aurum_ai_app")
            await owner.execute("SELECT set_config('app.current_org_id',$1,true)", str(org))
            await owner.execute("""INSERT INTO conversations(id,org_id,user_id,created_at,updated_at,last_message_at,
                title,model,status,cost_usd) VALUES($1,$2,$3,$4,$4,$4,'Lifecycle images','lifecycle-test','active',0)""",
                conversation, org, user, at)
            await owner.execute("""INSERT INTO interaction_turns(id,org_id,conversation_id,owner_id,status,limits,
                image_count,terminal_at,created_at,updated_at) VALUES($1,$2,$3,$4,'completed','{}',2,$5,$5,$5)""",
                turn, org, conversation, user, at)
            await owner.execute("""INSERT INTO interaction_executions(id,org_id,turn_id,workflow_id,run_id,task_queue,phase)
                VALUES($1,$2,$3,$4,$5,'lifecycle','ordinary')""", execution, org, turn, f"conversation-{conversation}", run)
            for name, kind in (("generated", "image_generate"), ("edited", "image_edit")):
                operation = UUID(state["operations"][name])
                await owner.execute("""INSERT INTO tool_operations(id,org_id,turn_id,execution_id,invocation_id,kind,
                    arguments_hash,status,result) VALUES($1,$2,$3,$4,$1,$5,$6,'completed',$7)""",
                    operation, org, turn, execution, kind, "d" * 64, json.dumps({"imageId": state["images"][name]}))
            rows = (
                ("generated", UUID(state["operations"]["generated"]), "generated", None, "image/png"),
                ("edited", UUID(state["operations"]["edited"]), "generated", UUID(state["images"]["generated"]), "image/png"),
                ("uploaded", None, "uploaded", None, "image/jpeg"),
            )
            for name, operation, origin, source, content_type in rows:
                await owner.execute("""INSERT INTO conversation_images(id,org_id,conversation_id,turn_id,execution_id,
                    operation_id,origin,source_image_id,storage_key,content_type,size_bytes,prompt,model,created_at,updated_at)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,'a lighthouse','lifecycle-image',$12,$12)""",
                    UUID(state["images"][name]), org, conversation, turn, execution if operation else None,
                    operation, origin, source, state["keys"][name], content_type, len(IMAGES[name]), at)
        async with owner.transaction():
            # Receipts are the operator observer's to write, not the app's.
            await owner.execute("""INSERT INTO conversation_executions(id,org_id,conversation_id,generation,namespace,
                workflow_id,run_id,workflow_type,task_queue,terminal_status,closed_at,observed_at,tree_complete)
                VALUES($1,$2,$3,1,'default',$4,$5,'ConversationWorkflow','lifecycle','COMPLETED',$6,$6,true)""",
                uuid4(), org, conversation, f"conversation-{conversation}", run, at)
            await owner.execute("UPDATE conversation_identities SET execution_provenance_complete=true WHERE id=$1", conversation)
    finally:
        await owner.close()
    storage = business_storage()
    for name, data in IMAGES.items():
        storage.put_object(Bucket=BUCKET, Key=state["keys"][name], Body=data)
    state["prepared"] = True
    save(state)
    print("Prepared an old conversation with 3 images (generated, edited, uploaded); private journal saved")


if __name__ == "__main__":
    try:
        asyncio.run(prepare())
    except Exception:
        raise SystemExit("Owned image fixture preparation failed; preserve journal and inspect exact fixture") from None
