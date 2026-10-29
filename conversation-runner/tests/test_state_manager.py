from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.state_manager import StateManager
from src.workflows.conversation import ConversationWorkflow


@pytest.fixture
def mock_client_with_response(monkeypatch: pytest.MonkeyPatch):
    def _install(response: MagicMock):
        client = MagicMock()
        client.__aenter__.return_value = client
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(return_value=response)
        monkeypatch.setattr("httpx.AsyncClient", MagicMock(return_value=client))
        return client

    return _install


@pytest.mark.asyncio
async def test_load_requests_correct_url_and_headers(mock_client_with_response, monkeypatch):
    monkeypatch.setattr("src.state_manager.settings.llm_gateway_url", "http://backend:8000")
    monkeypatch.setattr("src.state_manager.settings.conversation_id", "conv-1")
    monkeypatch.setattr("src.state_manager.settings.session_token", "tok-1")

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "data": {"messages": {"data": [{"role": "user", "content": "hi", "extra": "ignored"}]}}
    }
    client = mock_client_with_response(response)

    manager = StateManager()
    await manager.load()

    client.get.assert_awaited_once_with(
        "http://backend:8000/v1/conversations/conv-1",
        headers={"Authorization": "Bearer tok-1"},
    )
    assert manager.messages == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_load_raises_value_error_on_unexpected_shape(mock_client_with_response):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": {}}
    mock_client_with_response(response)

    manager = StateManager()
    with pytest.raises(ValueError, match="Unexpected conversation API response shape"):
        await manager.load()


@pytest.mark.asyncio
async def test_load_propagates_http_error(mock_client_with_response):
    response = MagicMock()
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("not found", request=None, response=None)
    )
    mock_client_with_response(response)

    manager = StateManager()
    with pytest.raises(httpx.HTTPStatusError):
        await manager.load()


@pytest.mark.asyncio
async def test_load_keeps_image_ids_of_user_messages_only(mock_client_with_response):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": {"messages": {"data": [
        {"role": "user", "content": "look", "turnId": "t1", "imageIds": ["img-1"]},
        {"role": "assistant", "content": "a cat", "turnId": "t1", "imageIds": ["img-2"]},
        {"role": "user", "content": "plain", "turnId": "t2", "imageIds": []},
    ]}}}
    mock_client_with_response(response)

    manager = StateManager()
    await manager.load()

    assert manager.messages == [
        {"role": "user", "content": "look", "turn_id": "t1", "image_ids": ["img-1"]},
        {"role": "assistant", "content": "a cat", "turn_id": "t1"},
        {"role": "user", "content": "plain", "turn_id": "t2"},
    ]


@pytest.mark.asyncio
async def test_load_leaves_a_failed_turns_reason_out_of_the_dialogue(mock_client_with_response):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"data": {"messages": {"data": [
        {"role": "user", "content": "plan a trip", "turnId": "t1"},
        {"role": "assistant", "content": "Could not get a model response.", "turnId": "t1", "failed": True},
        {"role": "user", "content": "again", "turnId": "t2"},
        {"role": "assistant", "content": "Here it is.", "turnId": "t2", "failed": False},
    ]}}}
    mock_client_with_response(response)

    manager = StateManager()
    await manager.load()

    assert manager.messages == [
        {"role": "user", "content": "plan a trip", "turn_id": "t1"},
        {"role": "user", "content": "again", "turn_id": "t2"},
        {"role": "assistant", "content": "Here it is.", "turn_id": "t2"},
    ]


def test_append_adds_message_to_list():
    manager = StateManager()
    manager.append({"role": "user", "content": "hi"})
    assert manager.messages == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_restored_turn_is_not_duplicated_by_its_queued_signal(mock_client_with_response):
    response = MagicMock()
    response.json.return_value = {"data": {"messages": {"data": [
        {"role": "user", "content": "hi", "turnId": "turn-1"},
    ]}}}
    mock_client_with_response(response)
    manager = StateManager()
    await manager.load()
    workflow = ConversationWorkflow()
    workflow.messages = manager.messages

    workflow._append_message_once({"role": "user", "content": "hi", "turn_id": "turn-1"})
    assert len(workflow.messages) == 1
    workflow._append_message_once({"role": "user", "content": "hi", "turn_id": "turn-2"})
    assert len(workflow.messages) == 2


@pytest.mark.asyncio
async def test_load_follows_history_cursor_to_restore_every_message(monkeypatch):
    requests = []
    first = [{"role": "user", "content": str(i)} for i in range(50)]
    last = [{"role": "assistant", "content": "last response"}]

    def respond(request):
        requests.append(request.url)
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(200, json={"data": {"messages": {
                "data": first, "pagination": {"hasMore": True, "cursor": "page-two"}
            }}})
        assert cursor == "page-two"
        return httpx.Response(200, json={"data": {"messages": {
            "data": last, "pagination": {"hasMore": False, "cursor": None}
        }}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr("httpx.AsyncClient", lambda: real_client(transport=httpx.MockTransport(respond)))
    manager = StateManager()
    await manager.load()
    assert manager.messages == first + last
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [None, "same"])
async def test_load_rejects_nonadvancing_pagination_without_partial_state(monkeypatch, cursor):
    def respond(request):
        return httpx.Response(200, json={"data": {"messages": {
            "data": [{"role": "user", "content": "partial"}],
            "pagination": {"hasMore": True, "cursor": cursor}
        }}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr("httpx.AsyncClient", lambda: real_client(transport=httpx.MockTransport(respond)))
    manager = StateManager()
    manager.messages = [{"role": "user", "content": "previous"}]
    with pytest.raises(ValueError, match="cursor"):
        await manager.load()
    assert manager.messages == [{"role": "user", "content": "previous"}]
