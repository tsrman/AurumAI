import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.models.file_metadata import FileMetadata
from src.models.organization import Organization
from src.services.document_cleanup import DocumentCleanupService


ORG_ID = uuid.uuid4()
KNOWLEDGE_BASE_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
STORAGE_KEY = f"documents/{ORG_ID}/{KNOWLEDGE_BASE_ID}/{DOCUMENT_ID}.txt"


class FakeResult:
    def __init__(self, value: FileMetadata | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> FileMetadata | None:
        return self.value


class FakeSession:
    async def scalar(self, statement):
        return None

    def __init__(self, metadata: FileMetadata | None, events: list[str]) -> None:
        self.metadata = metadata
        self.events = events
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return FakeResult(self.metadata)

    async def delete(self, metadata: FileMetadata) -> None:
        assert metadata is self.metadata
        self.events.append("metadata")

    async def commit(self) -> None:
        self.events.append("commit")


class FakeStorage:
    def __init__(self, events: list[str], failure: Exception | None = None) -> None:
        self.events = events
        self.failure = failure
        self.keys: list[str] = []

    async def delete(self, key: str) -> None:
        self.keys.append(key)
        self.events.append("storage")
        if self.failure is not None:
            raise self.failure


class FakeRag:
    def __init__(self, events: list[str], failure: Exception | None = None) -> None:
        self.events = events
        self.failure = failure
        self.calls: list[tuple[str, str, str]] = []

    async def delete_document(
        self,
        org_id: str,
        knowledge_base_id: str,
        document_id: str,
    ) -> None:
        self.calls.append((org_id, knowledge_base_id, document_id))
        self.events.append("rag")
        if self.failure is not None:
            raise self.failure


def file_metadata(*, org_id: uuid.UUID = ORG_ID) -> FileMetadata:
    return FileMetadata(
        org_id=org_id,
        storage_key=STORAGE_KEY,
        filename="owned.txt",
        content_type="text/plain",
        size_bytes=12,
        document_id=DOCUMENT_ID,
    )


@pytest.mark.asyncio
async def test_file_delete_orders_storage_before_rag_before_metadata() -> None:
    events: list[str] = []
    metadata = file_metadata()
    session = FakeSession(metadata, events)
    storage = FakeStorage(events)
    rag = FakeRag(events)

    await DocumentCleanupService(storage=storage, rag=rag).delete(
        session,
        org_id=ORG_ID,
        knowledge_base_id=KNOWLEDGE_BASE_ID,
        document_id=str(DOCUMENT_ID),
    )

    assert events == ["storage", "rag", "metadata", "commit"]
    assert storage.keys == [STORAGE_KEY]
    assert rag.calls == [(str(ORG_ID), str(KNOWLEDGE_BASE_ID), str(DOCUMENT_ID))]


@pytest.mark.asyncio
async def test_storage_failure_preserves_rag_document_and_metadata() -> None:
    events: list[str] = []
    metadata = file_metadata()
    session = FakeSession(metadata, events)
    storage = FakeStorage(events, RuntimeError("minio unavailable"))
    rag = FakeRag(events)

    with pytest.raises(RuntimeError, match="minio unavailable"):
        await DocumentCleanupService(storage=storage, rag=rag).delete(
            session,
            org_id=ORG_ID,
            knowledge_base_id=KNOWLEDGE_BASE_ID,
            document_id=str(DOCUMENT_ID),
        )

    assert events == ["storage"]
    assert rag.calls == []


@pytest.mark.asyncio
async def test_rag_failure_preserves_metadata_for_retry() -> None:
    events: list[str] = []
    metadata = file_metadata()
    session = FakeSession(metadata, events)
    rag_failure = HTTPException(status_code=500, detail="rag failed")

    with pytest.raises(HTTPException) as raised:
        await DocumentCleanupService(
            storage=FakeStorage(events),
            rag=FakeRag(events, rag_failure),
        ).delete(
            session,
            org_id=ORG_ID,
            knowledge_base_id=KNOWLEDGE_BASE_ID,
            document_id=str(DOCUMENT_ID),
        )

    assert raised.value is rag_failure
    assert events == ["storage", "rag"]


@pytest.mark.asyncio
async def test_rag_not_found_with_metadata_finishes_partial_cleanup() -> None:
    events: list[str] = []
    metadata = file_metadata()
    session = FakeSession(metadata, events)

    await DocumentCleanupService(
        storage=FakeStorage(events),
        rag=FakeRag(events, HTTPException(status_code=404, detail="missing")),
    ).delete(
        session,
        org_id=ORG_ID,
        knowledge_base_id=KNOWLEDGE_BASE_ID,
        document_id=str(DOCUMENT_ID),
    )

    assert events == ["storage", "rag", "metadata", "commit"]


@pytest.mark.asyncio
async def test_rag_not_found_without_metadata_remains_not_found() -> None:
    events: list[str] = []
    session = FakeSession(None, events)
    rag_failure = HTTPException(status_code=404, detail="missing")

    with pytest.raises(HTTPException) as raised:
        await DocumentCleanupService(
            storage=FakeStorage(events),
            rag=FakeRag(events, rag_failure),
        ).delete(
            session,
            org_id=ORG_ID,
            knowledge_base_id=KNOWLEDGE_BASE_ID,
            document_id=str(DOCUMENT_ID),
        )

    assert raised.value is rag_failure
    assert events == ["rag"]


@pytest.mark.asyncio
@pytest.mark.parametrize("rag_missing", [False, True])
async def test_wrong_knowledge_base_cannot_delete_file_or_metadata(rag_missing) -> None:
    events: list[str] = []
    session = FakeSession(file_metadata(), events)
    rag_failure = HTTPException(status_code=404, detail="wrong scope") if rag_missing else None
    with pytest.raises(HTTPException) as raised:
        await DocumentCleanupService(
            storage=FakeStorage(events), rag=FakeRag(events, rag_failure)
        ).delete(
            session, org_id=ORG_ID, knowledge_base_id=uuid.uuid4(),
            document_id=str(DOCUMENT_ID),
        )
    assert raised.value.status_code == 404
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_key", [
    "documents/legacy-file.txt", "unknown/object", "",
    f"documents/{ORG_ID}/{KNOWLEDGE_BASE_ID}-other/file.txt",
])
async def test_unproven_storage_scope_fails_closed(storage_key) -> None:
    events: list[str] = []
    metadata = file_metadata()
    metadata.storage_key = storage_key
    with pytest.raises(HTTPException) as raised:
        await DocumentCleanupService(
            storage=FakeStorage(events), rag=FakeRag(events)
        ).delete(
            FakeSession(metadata, events), org_id=ORG_ID,
            knowledge_base_id=KNOWLEDGE_BASE_ID, document_id=str(DOCUMENT_ID),
        )
    assert raised.value.status_code == 404
    assert events == []


@pytest.mark.asyncio
async def test_metadata_lookup_cannot_cross_organization(db, test_org) -> None:
    other_org = Organization(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
    db.add(other_org)
    await db.flush()
    foreign_metadata = file_metadata(org_id=other_org.id)
    db.add(foreign_metadata)
    await db.flush()
    events: list[str] = []
    rag_failure = HTTPException(status_code=404, detail="missing")

    with pytest.raises(HTTPException) as raised:
        await DocumentCleanupService(
            storage=FakeStorage(events),
            rag=FakeRag(events, rag_failure),
        ).delete(
            db,
            org_id=test_org.id,
            knowledge_base_id=KNOWLEDGE_BASE_ID,
            document_id=str(DOCUMENT_ID),
        )

    assert raised.value is rag_failure
    assert events == ["rag"]
    assert (
        await db.execute(
            select(FileMetadata).where(FileMetadata.id == foreign_metadata.id)
        )
    ).scalar_one() is foreign_metadata
