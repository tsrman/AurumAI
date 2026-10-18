"""Lifecycle transactions preserve uncertain work and serialize archive claims."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from src.models.conversation import Conversation
from src.models.data_identity import ConversationIdentity


@pytest.mark.asyncio
async def test_pending_external_operation_blocks_claim_even_after_time_passes(
    db, test_org, test_user
):
    from src.services.conversation_lifecycle import (
        ArchiveBlocked,
        begin_operation,
        claim_conversation,
    )

    conv = Conversation(org_id=test_org.id, user_id=test_user.id, model="test")
    db.add(conv)
    await db.flush()
    operation = await begin_operation(
        db, test_org.id, conv.id, kind="document_upload", operation_key=uuid.uuid4()
    )
    await db.commit()
    with pytest.raises(ArchiveBlocked, match="unsettled_operation"):
        await claim_conversation(
            db,
            test_org.id,
            conv.id,
            archive_id=uuid.uuid4(),
            lease_id=uuid.uuid4(),
            cutoff=datetime.now(timezone.utc) + timedelta(days=1),
        )
    assert operation.status == "pending"


@pytest.mark.asyncio
async def test_claim_and_read_visibility_for_empty_conversation(db, test_org, test_user):
    from src.services.conversation_lifecycle import (
        ConversationFrozen,
        claim_conversation,
        lock_active_conversation,
    )
    from src.repositories.conversation_repository import conversation_repository

    conv = Conversation(org_id=test_org.id, user_id=test_user.id, model="test")
    db.add(conv)
    await db.flush()
    await claim_conversation(
        db,
        test_org.id,
        conv.id,
        archive_id=uuid.uuid4(),
        lease_id=uuid.uuid4(),
        cutoff=datetime.now(timezone.utc) + timedelta(days=1),
    )
    await db.commit()
    with pytest.raises(ConversationFrozen):
        await lock_active_conversation(db, test_org.id, conv.id)
    assert await conversation_repository.get(db, str(conv.id)) is None
    rows, _ = await conversation_repository.list_by_user(db, str(test_user.id), str(test_org.id))
    assert rows == []
    identity = await db.scalar(
        select(ConversationIdentity).where(ConversationIdentity.id == conv.id)
    )
    assert identity.lifecycle == "archiving"


@pytest.mark.asyncio
async def test_stale_session_fails_again_after_commit_and_restore_generation(
    db, test_org, test_user
):
    from src.services.conversation_lifecycle import ConversationFrozen, lock_active_conversation

    conv = Conversation(org_id=test_org.id, user_id=test_user.id, model="test")
    db.add(conv)
    await db.flush()
    await lock_active_conversation(db, test_org.id, conv.id, generation=1)
    await db.commit()
    identity = await db.scalar(
        select(ConversationIdentity).where(ConversationIdentity.id == conv.id)
    )
    identity.generation = 3
    identity.restored_at = datetime.now(timezone.utc)
    await db.commit()
    with pytest.raises(ConversationFrozen):
        await lock_active_conversation(db, test_org.id, conv.id, generation=1)
    assert (await lock_active_conversation(db, test_org.id, conv.id, generation=3)).generation == 3


@pytest.mark.asyncio
async def test_later_closed_root_never_erases_legacy_provenance_gap(db, test_org, test_user):
    from src.models.archive_control import ConversationExecution
    from src.models.message import Message
    from src.services.conversation_lifecycle import eligibility_blocker

    conv = Conversation(org_id=test_org.id, user_id=test_user.id, model="test")
    db.add(conv)
    await db.flush()
    db.add(Message(org_id=test_org.id, conversation_id=conv.id, role="user", content="legacy"))
    await db.flush()
    identity = await db.scalar(
        select(ConversationIdentity).where(ConversationIdentity.id == conv.id)
    )
    identity.execution_provenance_complete = False
    now = datetime.now(timezone.utc)
    db.add(
        ConversationExecution(
            org_id=test_org.id,
            conversation_id=conv.id,
            generation=1,
            namespace="default",
            workflow_id=f"conversation-{conv.id}",
            run_id=uuid.uuid4(),
            workflow_type="ConversationWorkflow",
            task_queue=f"session-{conv.id}",
            terminal_status="COMPLETED",
            closed_at=now,
            observed_at=now,
            tree_complete=True,
        )
    )
    await db.flush()
    assert (
        await eligibility_blocker(db, identity, conv, now + timedelta(days=1))
        == "legacy_execution_provenance_missing"
    )

@pytest.mark.asyncio
async def test_attachment_admission_updates_activity_only_for_new_operation(db,test_org,test_user):
    from src.models.knowledge_base import KnowledgeBase
    from src.services.conversation_lifecycle import begin_attachment_operation
    old=datetime(2020,2,29,tzinfo=timezone.utc)
    conv=Conversation(org_id=test_org.id,user_id=test_user.id,model='test',created_at=old,updated_at=old)
    db.add(conv)
    await db.flush()
    kb=KnowledgeBase(org_id=test_org.id,name='attachment activity',owning_conversation_id=conv.id)
    db.add(kb)
    await db.commit()
    key=uuid.uuid4()
    await begin_attachment_operation(db,test_org.id,kb.id,'document_upload',key)
    await db.refresh(conv)
    first=conv.updated_at
    assert first>old
    await begin_attachment_operation(db,test_org.id,kb.id,'document_upload',key)
    await db.refresh(conv)
    assert conv.updated_at==first
