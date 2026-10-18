import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.asyncio as redis
import redis.exceptions as redis_exceptions

from src.event_publisher import EventPublisher


@pytest.mark.asyncio
async def test_publish_sends_correct_channel_and_payload(monkeypatch):
    monkeypatch.setattr("src.event_publisher.settings.conversation_id", "conv-1")
    monkeypatch.setattr("src.event_publisher.settings.org_id", "org-1")
    publisher = EventPublisher()
    fake_client = MagicMock()
    fake_client.publish = AsyncMock()
    publisher._client = fake_client

    await publisher.publish("message-start", {"role": "assistant"})

    fake_client.publish.assert_awaited_once_with(
        "org-1:session:conv-1:events",
        json.dumps({"type": "message-start", "data": {"role": "assistant"}}),
    )


@pytest.mark.asyncio
async def test_publish_channel_falls_back_to_sentinel_when_org_id_unset(monkeypatch):
    # Matches the "-" sentinel convention used elsewhere for an unset org_id
    # (e.g. session_manager.py's session_field) — shouldn't happen in
    # practice (publish only ever runs after /bind sets org_id), but the
    # fallback must not crash if it ever does.
    monkeypatch.setattr("src.event_publisher.settings.conversation_id", "conv-1")
    monkeypatch.setattr("src.event_publisher.settings.org_id", None)
    publisher = EventPublisher()
    fake_client = MagicMock()
    fake_client.publish = AsyncMock()
    publisher._client = fake_client

    await publisher.publish("message-start", {"role": "assistant"})

    fake_client.publish.assert_awaited_once_with(
        "-:session:conv-1:events",
        json.dumps({"type": "message-start", "data": {"role": "assistant"}}),
    )


@pytest.mark.asyncio
async def test_publish_includes_turn_id_at_top_level_when_present(monkeypatch):
    monkeypatch.setattr("src.event_publisher.settings.conversation_id", "conv-1")
    publisher = EventPublisher()
    fake_client = MagicMock()
    fake_client.publish = AsyncMock()
    publisher._client = fake_client

    await publisher.publish("content-delta", {"delta": "hi", "turn_id": "t1"})

    sent_payload = json.loads(fake_client.publish.call_args.args[1])
    assert sent_payload["turn_id"] == "t1"
    assert sent_payload["data"] == {"delta": "hi", "turn_id": "t1"}


@pytest.mark.asyncio
async def test_publish_swallows_redis_error(monkeypatch):
    monkeypatch.setattr("src.event_publisher.settings.conversation_id", "conv-1")
    publisher = EventPublisher()
    fake_client = MagicMock()
    fake_client.publish = AsyncMock(side_effect=redis_exceptions.ConnectionError("down"))
    publisher._client = fake_client

    await publisher.publish("done", {"turn_id": "t1"})  # must not raise


@pytest.mark.asyncio
async def test_client_property_creates_redis_client_lazily(monkeypatch):
    monkeypatch.setattr("src.event_publisher.settings.redis_url", "redis://test:6379/0")
    created = MagicMock()
    monkeypatch.setattr("src.event_publisher.redis.from_url", MagicMock(return_value=created))

    publisher = EventPublisher()
    assert publisher._client is None
    client = publisher.client
    assert client is created
    # Second access must not create a new client.
    assert publisher.client is created


@pytest.mark.asyncio
async def test_close_closes_and_resets_client():
    publisher = EventPublisher()
    fake_client = MagicMock()
    fake_client.aclose = AsyncMock()
    publisher._client = fake_client

    await publisher.close()

    fake_client.aclose.assert_awaited_once()
    assert publisher._client is None


@pytest.mark.asyncio
async def test_close_is_noop_when_no_client_created():
    publisher = EventPublisher()
    await publisher.close()  # must not raise
    assert publisher._client is None


def test_client_property_builds_a_real_asyncio_redis_client():
    """
    Regression test: `import redis.asyncio as redis` followed by a bare
    `import redis.exceptions` (no `as`) rebinds the module-level `redis` name
    back to the top-level *synchronous* package (Python binds dotted imports
    to their top-level name unless aliased). That made `self.client` build a
    sync `redis.client.Redis`, whose `.publish()` returns a plain int instead
    of a coroutine — every `await self.client.publish(...)` then raised
    `TypeError: object int can't be used in 'await' expression`. Every other
    test here mocks `_client` directly and would not catch this; this one
    doesn't, so it pins the real construction path.
    """
    publisher = EventPublisher()
    assert isinstance(publisher.client, redis.Redis)  # redis.asyncio.Redis


@pytest.mark.asyncio
async def test_publish_promotes_camel_case_turn_id_for_workflow_events(monkeypatch):
    """Workflow progress payloads are projected onto the frontend's camelCase
    allowlist, so they carry turnId rather than turn_id.  Without promoting it
    the envelope has no turn at all, and the backend relay — which only drops
    events whose turn_id mismatches — forwards them onto every open stream for
    the conversation."""
    monkeypatch.setattr("src.event_publisher.settings.conversation_id", "conv-1")
    publisher = EventPublisher()
    fake_client = MagicMock()
    fake_client.publish = AsyncMock()
    publisher._client = fake_client

    await publisher.publish(
        "workflow-node-status",
        {"turnId": "t1", "workflowRunId": "r1", "nodeKey": "a", "status": "running"},
    )

    sent_payload = json.loads(fake_client.publish.call_args.args[1])
    assert sent_payload["turn_id"] == "t1"
    # The payload itself stays on the safe camelCase allowlist.
    assert "turn_id" not in sent_payload["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type,data,expected",
    [
        (
            "question-status",
            {
                "questionId": "q1",
                "turnId": "t1",
                "status": "pending",
                "version": 1,
                "text": "private question",
                "answer": "private answer",
                "toolResults": {"secret": "hidden"},
            },
            {
                "questionId": "q1",
                "turnId": "t1",
                "status": "pending",
                "version": 1,
            },
        ),
        (
            "turn-status",
            {
                "turnId": "t1",
                "status": "completed",
                "version": 2,
                "prompt": "private prompt",
            },
            {"turnId": "t1", "status": "completed", "version": 2},
        ),
    ],
)
async def test_status_events_are_projected_to_metadata_allowlist(
    event_type, data, expected, monkeypatch
):
    monkeypatch.setattr("src.event_publisher.settings.conversation_id", "conv-1")
    publisher = EventPublisher()
    fake_client = MagicMock()
    fake_client.publish = AsyncMock()
    publisher._client = fake_client

    await publisher.publish(event_type, data)

    sent_payload = json.loads(fake_client.publish.call_args.args[1])
    assert sent_payload["data"] == expected
    assert sent_payload["turn_id"] == "t1"
