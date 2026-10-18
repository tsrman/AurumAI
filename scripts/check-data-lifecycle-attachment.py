"""Independent source/SQL/vector acceptance for one journaled test attachment."""

import asyncio
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
from uuid import UUID

import asyncpg
import boto3
from botocore.exceptions import ClientError
from botocore.config import Config
from pymilvus import MilvusClient

STATE = Path("/tmp/phaseb-lifecycle-acceptance/archive-fixture.json")
PROJECT = "phaseb-lifecycle-test"
FIELDS = ["pk", "org_id", "knowledge_base_id", "document_id", "chunk_index", "content",
          "dense", "parent_content", "acl_json", "document_title"]


def owned(service, container_port, host_port):
    result = subprocess.run(["docker", "inspect", f"{PROJECT}-{service}-1"],
                            check=True, capture_output=True, text=True, timeout=10)
    info = json.loads(result.stdout)[0]
    labels = info["Config"]["Labels"]
    if (labels.get("com.docker.compose.project") != PROJECT
            or labels.get("com.docker.compose.service") != service
            or info.get("State", {}).get("Running") is not True
            or info["NetworkSettings"]["Ports"].get(container_port) != [
                {"HostIp": "127.0.0.1", "HostPort": host_port}]):
        raise ValueError("owned_dependency_required")
    return info["Id"]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str, allow_nan=False).encode()).hexdigest()


def vector_digest(rows, org, kb, document):
    normalized = []
    for row in sorted(rows, key=lambda r: r["pk"]):
        if (row["org_id"], row["knowledge_base_id"], row["document_id"]) != (org, kb, document):
            raise ValueError("vector_scope_mismatch")
        if row["pk"] != f"{document}:{row['chunk_index']}" or len(row["dense"]) != 1536:
            raise ValueError("vector_identity_mismatch")
        dense = [float(value) for value in row["dense"]]
        if not all(math.isfinite(value) for value in dense):
            raise ValueError("nonfinite_fixture_vector")
        normalized.append({**row, "dense": hashlib.sha256(struct.pack("<1536f", *dense)).hexdigest()})
    return digest(normalized)


def verify_retained_proof(attachment, execution, collection_id):
    if (execution is None or execution["run_id"] is None
            or (execution["terminal_status"] or "").lower() != "completed"
            or execution["namespace"] != "default"
            or execution["workflow_id"] != attachment["workflow_id"]
            or execution["generation"] != attachment["generation"]):
        raise ValueError("retained_attachment_proof_missing")
    baseline = attachment.get("storage_baseline")
    if baseline and (baseline["index_run_id"] != str(execution["run_id"])
                     or baseline["index_namespace"] != execution["namespace"]
                     or baseline["collection_id"] != collection_id):
        raise ValueError("retained_attachment_proof_changed")


async def check(stage):
    state = json.loads(STATE.read_text())
    if owned("postgres", "5432/tcp", "55434") != state["postgres_container_id"]:
        raise ValueError("established_database_mismatch")
    owned("minio", "9000/tcp", "59000")
    owned("milvus", "19530/tcp", "59530")
    attachment = state["attachment"]
    org, kb, document = (str(UUID(value)) for value in (
        state["org_id"], attachment["knowledge_base_id"], attachment["document_id"]))
    if not attachment["source_key"].startswith(f"documents/{org}/{kb}/"):
        raise ValueError("source_scope_mismatch")
    source_name = attachment["source_key"].removeprefix(f"documents/{org}/{kb}/")
    if not source_name.endswith(".txt") or str(UUID(source_name[:-4])) + ".txt" != source_name:
        raise ValueError("source_identity_mismatch")
    if type(attachment["size"]) is not int or not 0 < attachment["size"] <= 1024 * 1024:
        raise ValueError("bounded_fixture_source_required")
    source = boto3.client("s3", endpoint_url="http://127.0.0.1:59000",
                          aws_access_key_id="lifecycle-business",
                          aws_secret_access_key="lifecycle-business-password", region_name="us-east-1",
                          config=Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 2}))
    try:
        obj = source.get_object(Bucket="lifecycle-business", Key=attachment["source_key"])
    except ClientError as error:
        if error.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
            raise ValueError("source_read_failed") from None
        source_hash = None
    else:
        try:
            raw = obj["Body"].read(attachment["size"] + 1)
            if len(raw) != attachment["size"]:
                raise ValueError("source_size_changed")
            source_hash = hashlib.sha256(raw).hexdigest()
        finally:
            obj["Body"].close()
    logging.getLogger("pymilvus").disabled = True
    milvus = MilvusClient(uri="http://127.0.0.1:59530", timeout=10)
    expression = f'org_id == "{org}" and knowledge_base_id == "{kb}" and document_id == "{document}"'
    try:
        count = milvus.query("documents", filter=expression, output_fields=["count(*)"],
                             consistency_level="Strong", timeout=10)[0]["count(*)"]
        if not 0 <= count <= 256:
            raise ValueError("bounded_fixture_required")
        rows = milvus.query("documents", filter=expression, output_fields=FIELDS,
                            limit=257, consistency_level="Strong", timeout=10)
        if len(rows) != count:
            raise ValueError("vector_inventory_unstable")
        collection_id = str(milvus.describe_collection("documents", timeout=10)["collection_id"])
    finally:
        milvus.close()
    connection = await asyncpg.connect("postgresql://lifecycle_rag_operator:lifecycle-rag-operator-password@127.0.0.1:55434/rag_platform", timeout=10)
    try:
        await connection.execute("SELECT set_config('app.current_org_id',$1,false)", org)
        row = await connection.fetchrow("SELECT * FROM documents WHERE id=$1 AND org_id=$2 AND knowledge_base_id=$3",
                                        UUID(document), UUID(org), UUID(kb))
        execution = await connection.fetchrow("SELECT run_id,workflow_id,namespace,generation,terminal_status FROM rag_index_executions WHERE operation_id=$1 AND org_id=$2 AND knowledge_base_id=$3 AND document_id=$4",
                                              UUID(attachment["operation_id"]), UUID(org), UUID(kb), UUID(document))
    finally:
        await connection.close()
    verify_retained_proof(attachment, execution, collection_id)
    if stage == "archived":
        if "storage_baseline" not in attachment or source_hash is not None or count != 0 or row is not None:
            raise ValueError("attachment_payload_not_purged")
        if attachment["storage_baseline"]["source_key_sha256"] != digest(attachment["source_key"]):
            raise ValueError("established_source_identity_changed")
        print("Owned attachment SQL/source/vectors absent after archive")
        return
    if (row is None or row["status"] != "indexed" or count <= 0
            or row["chunk_count"] != count or source_hash != attachment["sha256"]
            or row["source_key"] != attachment["source_key"]
            or row["source_filename"] != attachment["filename"]):
        raise ValueError("indexed_attachment_and_terminal_receipt_required")
    baseline = {"source_sha256": source_hash, "source_key_sha256": digest(attachment["source_key"]),
                "document_sha256": digest(dict(row)),
                "vectors_sha256": vector_digest(rows, org, kb, document),
                "vector_count": count, "collection_id": collection_id,
                "index_run_id": str(execution["run_id"]), "index_namespace": execution["namespace"]}
    if stage == "before" and "storage_baseline" not in attachment:
        attachment["storage_baseline"] = baseline
        temporary = STATE.with_suffix(".storage-tmp")
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
            json.dump(state, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, STATE)
    elif attachment.get("storage_baseline") != baseline:
        raise ValueError("original_attachment_state_not_preserved")
    print(f"Owned attachment verified: original source/SQL, {count} float32 vectors, unchanged collection")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in {"before", "archived", "restored"}:
            raise ValueError("explicit_stage_required")
        asyncio.run(check(sys.argv[1]))
    except Exception:
        raise SystemExit("Owned attachment verification blocked; preserve exact fixture and private journal") from None
