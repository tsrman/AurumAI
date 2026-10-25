"""Independent exact check of conversation images across archive and restore.

before:   rows and platform bytes are recorded as the baseline.
archived: no image rows remain, platform objects are gone and every archived copy
          (archives/v1/<org>/<archive>/images/<image>) has the baseline bytes.
restored: rows equal the baseline (including the edit's source reference) and the
          platform bytes are back; the conversation has a newer generation.
Rows are read with the lifecycle operator login, objects with the scoped accounts.
"""

import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from uuid import UUID

import asyncpg
import boto3
from botocore.exceptions import ClientError

STATE = Path("/tmp/phaseb-lifecycle-acceptance/images-fixture.json")
COLUMNS = ("id,org_id,conversation_id,turn_id,execution_id,operation_id,origin,source_image_id,"
           "storage_key,content_type,size_bytes,width,height,prompt,model,created_at,updated_at")


def storage(user: str):
    return boto3.client("s3", endpoint_url="http://127.0.0.1:59000", aws_access_key_id=f"lifecycle-{user}",
                        aws_secret_access_key=f"lifecycle-{user}-password", region_name="us-east-1")


def object_sha(client, bucket: str, key: str) -> str | None:
    try:
        response = client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise
    with response["Body"] as body:
        return hashlib.sha256(body.read()).hexdigest()


def digest(rows) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def save(state) -> None:
    temporary = STATE.with_suffix(".check-tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        json.dump(state, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, STATE)


async def check(stage: str) -> None:
    spec = importlib.util.spec_from_file_location(
        "history_fixture", Path(__file__).with_name("prepare-data-lifecycle-fixture.py")
    )
    history = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(history)
    state = json.loads(STATE.read_text())
    if not state.get("prepared") or history.owned_postgres() != state["postgres_container_id"]:
        raise ValueError("established_fixture_mismatch")
    org, conversation = UUID(state["org_id"]), UUID(state["conversation_id"])
    connection = await asyncpg.connect(
        "postgresql://lifecycle_data_operator:lifecycle-data-operator-password@127.0.0.1:55434/aurum_ai", timeout=10
    )
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            await connection.execute("SET LOCAL ROLE aurum_ai_lifecycle")
            await connection.execute("SELECT set_config('app.current_org_id',$1,true)", str(org))
            rows = [dict(row) for row in await connection.fetch(
                f"SELECT {COLUMNS} FROM conversation_images WHERE org_id=$1 AND conversation_id=$2 ORDER BY id",
                org, conversation)]
            identity = await connection.fetchrow(
                "SELECT lifecycle,generation,archive_id,restored_at FROM conversation_identities WHERE org_id=$1 AND id=$2",
                org, conversation)
    finally:
        await connection.close()
    if identity is None:
        raise ValueError("conversation_identity_missing")
    business = storage("business")
    platform = {name: object_sha(business, "lifecycle-business", key) for name, key in state["keys"].items()}
    baseline = state.get("images_baseline")
    if stage == "before":
        if (baseline is not None or identity["lifecycle"] != "active" or identity["archive_id"] is not None
                or len(rows) != 3 or platform != state["sha256"]):
            raise ValueError("original_image_fixture_required")
        state["images_baseline"] = {"rows": digest(rows), "generation": identity["generation"]}
        save(state)
    elif stage == "archived":
        if baseline is None or identity["lifecycle"] != "archived" or identity["archive_id"] is None:
            raise ValueError("archived_conversation_required")
        if identity["generation"] <= baseline["generation"]:
            raise ValueError("archive_generation_not_advanced")
        if rows or any(value is not None for value in platform.values()):
            raise ValueError("image_payload_not_purged")
        archives = storage("archives")
        prefix = f"archives/v1/{org}/{identity['archive_id']}/images"
        archived = {name: object_sha(archives, "lifecycle-archives", f"{prefix}/{state['images'][name]}")
                    for name in state["images"]}
        if archived != state["sha256"]:
            raise ValueError("archived_image_bytes_mismatch")
        baseline.update(archive_id=str(identity["archive_id"]), archive_generation=identity["generation"])
        save(state)
    else:
        if baseline is None or "archive_id" not in baseline:
            raise ValueError("archived_stage_required")
        if (identity["lifecycle"] != "active" or identity["restored_at"] is None
                or str(identity["archive_id"]) != baseline["archive_id"]
                or identity["generation"] <= baseline["archive_generation"]):
            raise ValueError("restored_conversation_required")
        if digest(rows) != baseline["rows"]:
            raise ValueError("restored_image_rows_mismatch")
        if platform != state["sha256"]:
            raise ValueError("restored_image_bytes_mismatch")
    print(f"Owned conversation images verified: {stage} ({len(rows)} rows)")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ("before", "archived", "restored"):
            raise ValueError("explicit_stage_required")
        asyncio.run(check(sys.argv[1]))
    except Exception:
        raise SystemExit("Owned image verification blocked; preserve private fixture journal") from None
