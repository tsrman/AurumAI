from typing import Any, AsyncGenerator

import httpx
import pytest
from temporalio.exceptions import ApplicationError

from src.llm_client import LLMClient


class FakeStreamResponse:
    def __init__(self, lines: list[str], status_error: Exception | None = None) -> None:
        self._lines = lines
        self._status_error = status_error
        self.aread_calls = 0

    @property
    def is_error(self) -> bool:
        return self._status_error is not None

    async def aread(self) -> bytes:
        self.aread_calls += 1
        return b""

    def raise_for_status(self) -> None:
        if self._status_error:
            raise self._status_error

    async def aiter_lines(self) -> AsyncGenerator[str, None]:
        for line in self._lines:
            yield line


class FakeStreamContext:
    def __init__(self, response: FakeStreamResponse) -> None:
        self._response = response

    async def __aenter__(self) -> FakeStreamResponse:
        return self._response

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


class FakeAsyncClient:
    last_instance: "FakeAsyncClient | None" = None
    response: FakeStreamResponse

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.stream_calls: list[dict[str, Any]] = []
        FakeAsyncClient.last_instance = self

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs: Any) -> FakeStreamContext:
        self.stream_calls.append({"method": method, "url": url, **kwargs})
        return FakeStreamContext(FakeAsyncClient.response)


@pytest.mark.asyncio
async def test_stream_chat_posts_correct_url_and_auth_header(monkeypatch):
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("src.llm_client.settings.llm_gateway_url", "https://gateway.example")
    monkeypatch.setattr("src.llm_client.settings.session_token", "session-tok")
    FakeAsyncClient.response = FakeStreamResponse(["data: [DONE]"])

    client = LLMClient()
    request = {"model": "gpt-4o-mini", "messages": []}
    _ = [chunk async for chunk in client.stream_chat(request)]

    call = FakeAsyncClient.last_instance.stream_calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://gateway.example/v1/chat/completions"
    assert call["json"] == request
    assert call["headers"] == {"Authorization": "Bearer session-tok"}


@pytest.mark.asyncio
async def test_stream_chat_yields_only_data_lines_stripped_of_prefix(monkeypatch):
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("src.llm_client.settings.llm_gateway_url", "https://gateway.example")
    monkeypatch.setattr("src.llm_client.settings.session_token", "session-tok")
    FakeAsyncClient.response = FakeStreamResponse(
        [
            "",
            "event: ping",
            'data: {"choices": [{"delta": {"content": "Hi"}}]}',
            'data: {"choices": [{"delta": {"content": " there"}}]}',
        ]
    )

    client = LLMClient()
    chunks = [chunk async for chunk in client.stream_chat({"model": "gpt-4o-mini"})]

    assert chunks == [
        '{"choices": [{"delta": {"content": "Hi"}}]}',
        '{"choices": [{"delta": {"content": " there"}}]}',
    ]


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://gateway.example/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.asyncio
async def test_stream_chat_propagates_5xx_as_retryable_http_error(monkeypatch):
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("src.llm_client.settings.llm_gateway_url", "https://gateway.example")
    monkeypatch.setattr("src.llm_client.settings.session_token", "session-tok")
    FakeAsyncClient.response = FakeStreamResponse([], status_error=_http_status_error(500))

    client = LLMClient()
    with pytest.raises(httpx.HTTPStatusError):
        _ = [chunk async for chunk in client.stream_chat({"model": "gpt-4o-mini"})]


@pytest.mark.asyncio
async def test_stream_chat_wraps_4xx_as_non_retryable(monkeypatch):
    """Regression test: without this, a persistent
    4xx (bad request, expired session token, ...) retries forever under
    Temporal's default retry policy and every future turn hangs on the
    same error."""
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("src.llm_client.settings.llm_gateway_url", "https://gateway.example")
    monkeypatch.setattr("src.llm_client.settings.session_token", "session-tok")
    FakeAsyncClient.response = FakeStreamResponse([], status_error=_http_status_error(422))

    client = LLMClient()
    with pytest.raises(ApplicationError) as exc_info:
        _ = [chunk async for chunk in client.stream_chat({"model": "gpt-4o-mini"})]
    assert exc_info.value.non_retryable


class _UnreadStream(httpx.AsyncByteStream):
    """An error body httpx has not consumed — what a streamed 4xx really
    looks like. httpx.Response(..., json=...) arrives pre-read and would
    make the test pass without the fix."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self) -> AsyncGenerator[bytes, None]:
        yield self._data


@pytest.mark.asyncio
async def test_stream_chat_error_carries_the_gateway_body(monkeypatch):
    """A real httpx client over MockTransport, not FakeAsyncClient: the
    behaviour under test is httpx's own streamed-response semantics — the
    body stays unread and `.text` raises ResponseNotRead until aread() is
    awaited — which a hand-written fake only reproduces if its author
    already knew about it.

    Without the body, the error is just "Client error '404 Not Found' for
    url ...", which never reveals that it was the *model* the gateway
    rejected. That message is what surfaces in the failed turn, so it is the
    only thing anyone debugging has to go on.
    """
    monkeypatch.setattr("src.llm_client.settings.llm_gateway_url", "https://gateway.example")
    monkeypatch.setattr("src.llm_client.settings.session_token", "session-tok")

    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, stream=_UnreadStream(b'{"error":{"code":"model_not_found"}}')
        )

    def make_client(**kwargs: Any) -> Any:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("httpx.AsyncClient", make_client)

    client = LLMClient()
    with pytest.raises(ApplicationError) as exc_info:
        _ = [chunk async for chunk in client.stream_chat({"model": "openai/text-embedding-3-small"})]

    assert exc_info.value.non_retryable
    assert "model_not_found" in str(exc_info.value)


@pytest.mark.asyncio
async def test_stream_chat_reads_settings_fresh_not_at_construction(monkeypatch):
    """Pool-mode containers construct `llm_client` at import time, before
    /bind assigns a real session_token — this must not get baked in."""
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeStreamResponse(["data: [DONE]"])

    client = LLMClient()  # constructed with no token available yet

    monkeypatch.setattr("src.llm_client.settings.llm_gateway_url", "https://gateway.example")
    monkeypatch.setattr("src.llm_client.settings.session_token", "bound-later-tok")
    _ = [chunk async for chunk in client.stream_chat({"model": "gpt-4o-mini"})]

    call = FakeAsyncClient.last_instance.stream_calls[0]
    assert call["headers"] == {"Authorization": "Bearer bound-later-tok"}
