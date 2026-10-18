"""Real aiobotocore transport uncertainty preserves the attachment intent."""

import asyncio
import io
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from aiobotocore.httpsession import AIOHTTPSession
from botocore.exceptions import ReadTimeoutError
from fastapi import UploadFile
from sqlalchemy import select

from src.core.config import settings
from src.models.conversation import Conversation
from src.models.knowledge_base import KnowledgeBase
from src.models.archive_control import ConversationOperation
from src.services import document_upload
from src.services.conversation_lifecycle import ArchiveBlocked, claim_conversation


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize(
    "live",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skipif(
                not os.environ.get("RAG_ARCHIVE_LIVE_TESTS"),
                reason="dedicated MinIO fixture required",
            ),
        ),
    ],
)
async def test_accepted_upload_loss_has_one_sdk_submission_and_blocks_archive(
    db, test_org, test_user, monkeypatch, cancel, live
):
    conv = Conversation(org_id=test_org.id, user_id=test_user.id, model="fixture")
    db.add(conv)
    await db.flush()
    kb = KnowledgeBase(org_id=test_org.id, name=str(uuid.uuid4()), owning_conversation_id=conv.id)
    db.add(kb)
    await db.commit()
    monkeypatch.setattr(
        settings, "S3_ENDPOINT", "http://127.0.0.1:59000" if live else "http://127.0.0.1:1"
    )
    monkeypatch.setattr(settings, "S3_ACCESS_KEY", "lifecycle-business" if live else "fixture")
    monkeypatch.setattr(
        settings, "S3_SECRET_KEY", "lifecycle-business-password" if live else "fixture"
    )
    monkeypatch.setattr(settings, "S3_BUCKET", "lifecycle-business" if live else "fixture")
    attempts = []
    original_send = AIOHTTPSession.send

    async def accepted(self, request):
        response = await original_send(self, request) if live else None
        if request.method != "PUT":
            return response
        attempts.append(request.method)
        if len(attempts) > 1:
            raise AssertionError("hidden_second_submission")
        if cancel:
            raise asyncio.CancelledError()
        raise ReadTimeoutError(endpoint_url="fixture")

    monkeypatch.setattr(AIOHTTPSession, "send", accepted)
    rag = AsyncMock()
    monkeypatch.setattr(document_upload.rag_client, "create_document", rag)
    with pytest.raises(asyncio.CancelledError if cancel else ReadTimeoutError):
        await document_upload.store_and_index_document(
            db,
            org_id=str(test_org.id),
            knowledge_base_id=kb.id,
            file=UploadFile(file=io.BytesIO(b"fixture"), filename="fixture.txt"),
            uploaded_by=str(test_user.id),
        )
    operation = await db.scalar(
        select(ConversationOperation).where(ConversationOperation.conversation_id == conv.id)
    )
    try:
        assert attempts == ["PUT"]
        rag.assert_not_awaited()
        assert operation.status == "pending"
        if live:
            assert (
                await document_upload.object_storage.get_bytes(operation.resource_key) == b"fixture"
            )
        with pytest.raises(ArchiveBlocked, match="unsettled_operation"):
            await claim_conversation(
                db,
                test_org.id,
                conv.id,
                archive_id=uuid.uuid4(),
                lease_id=uuid.uuid4(),
                cutoff=datetime.now(timezone.utc) + timedelta(days=1),
            )
    finally:
        if live and operation is not None:
            # Fixture saw the original response before injecting its loss;
            # this exact owned request is known completed, not a production ack.
            await document_upload.object_storage.delete(operation.resource_key)
