import importlib.util
import uuid
import pytest


def test_vector_snapshot_rejects_wrong_scope_and_preserves_float32():
    assert importlib.util.find_spec("src.archive") is not None
    from src.archive.vector_snapshot import validate_row
    from src.lifecycle import ArchiveBlocked, Scope

    scope = Scope(uuid.uuid4(), uuid.uuid4(), 2)
    document = uuid.uuid4()
    row = {
        "pk": f"{document}:0",
        "org_id": str(scope.org_id),
        "knowledge_base_id": str(scope.knowledge_base_id),
        "document_id": str(document),
        "chunk_index": 0,
        "content": "original",
        "dense": [0.25, -0.5],
        "parent_content": None,
        "acl_json": '["public"]',
        "document_title": "title",
    }
    assert validate_row(row, scope, document, 2)["dense"] == [0.25, -0.5]
    with pytest.raises(ArchiveBlocked, match="vector_scope_invalid"):
        validate_row({**row, "org_id": str(uuid.uuid4())}, scope, document, 2)
    with pytest.raises(ArchiveBlocked, match="vector_schema_invalid"):
        validate_row({**row, "secret_dynamic": "must not leak"}, scope, document, 2)


def test_parquet_roundtrip_preserves_null_uuid_and_utc():
    assert importlib.util.find_spec("src.archive") is not None
    from src.archive.storage import encode_rows, decode_rows
    from datetime import datetime, timezone

    row = {
        "id": str(uuid.uuid4()),
        "org_id": str(uuid.uuid4()),
        "knowledge_base_id": str(uuid.uuid4()),
        "title": None,
        "content": "Hello",
        "parsed_markdown": None,
        "source_key": None,
        "source_filename": None,
        "status": "indexed",
        "error": None,
        "chunk_count": 0,
        "created_at": datetime(2020, 2, 29, tzinfo=timezone.utc),
        "updated_at": datetime(2020, 2, 29, tzinfo=timezone.utc),
    }
    assert decode_rows(encode_rows([row], "documents"), "documents") == [row]
