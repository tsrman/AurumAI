"""An empty data plane must not hide lost receipt or replaced collection proof."""

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from botocore.exceptions import ClientError
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["receipt_missing", "run_changed", "collection_changed", "none"])
async def test_archived_stage_preserves_exact_execution_and_collection(damage, tmp_path, monkeypatch):
    collection_id = "original-collection" if damage != "collection_changed" else "replacement"
    milvus = SimpleNamespace(
        query=lambda *a, **kw: [{"count(*)": 0}] if kw["output_fields"] == ["count(*)"] else [],
        describe_collection=lambda *a, **kw: {"collection_id": collection_id}, close=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "pymilvus", SimpleNamespace(MilvusClient=lambda **kw: milvus))
    path = Path(__file__).parents[1] / "check-data-lifecycle-attachment.py"
    spec = importlib.util.spec_from_file_location("lifecycle_storage_checker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    org, kb, doc, operation, run = [str(uuid4()) for _ in range(5)]
    key = f"documents/{org}/{kb}/{uuid4()}.txt"
    workflow = f"rag-index-{doc}-{operation}"
    state = {"postgres_container_id": "owned-pg", "org_id": org, "attachment": {
        "knowledge_base_id": kb, "document_id": doc, "operation_id": operation,
        "generation": 1, "workflow_id": workflow, "source_key": key, "size": 12,
        "storage_baseline": {"source_key_sha256": module.digest(key),
                             "index_run_id": run, "index_namespace": "default",
                             "collection_id": "original-collection"},
    }}
    journal = tmp_path / "state.json"
    journal.write_text(json.dumps(state))
    monkeypatch.setattr(module, "STATE", journal)
    monkeypatch.setattr(module, "owned", lambda *a: "owned-pg")

    def absent(**kw):
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    monkeypatch.setattr(module.boto3, "client", lambda *a, **kw: SimpleNamespace(get_object=absent))
    execution = {"run_id": run if damage != "run_changed" else str(uuid4()),
                 "workflow_id": workflow, "namespace": "default", "generation": 1,
                 "terminal_status": "completed"}
    connection = AsyncMock()
    connection.fetchrow.side_effect = [None, None if damage == "receipt_missing" else execution]
    monkeypatch.setattr(module.asyncpg, "connect", AsyncMock(return_value=connection))
    if damage == "none":
        await module.check("archived")
    else:
        with pytest.raises(ValueError, match="retained_attachment_proof"):
            await module.check("archived")
    assert json.loads(journal.read_text()) == state
