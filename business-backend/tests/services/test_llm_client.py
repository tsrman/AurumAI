import json
from typing import Any, AsyncGenerator

import httpx
import pytest

from src.services.llm_client import LLMClient


class FakeStreamResponse:
    def __init__(self, lines: list[str], status_error: Exception | None = None) -> None:
        self._lines = lines
        self._status_error = status_error
        self.aread_calls = 0

    @property
    def is_error(self) -> bool:
        return self._status_error is not None

    async def aread(self) -> bytes:
        # Present because production calls it before raising, on error
        # responses only — see the streamed-body tests below.
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


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("src.services.llm_client.asyncio.sleep", instant_sleep)


@pytest.mark.asyncio
async def test_mock_stream_yields_word_chunks_then_final_stop_chunk() -> None:
    client = LLMClient()
    chunks = [json.loads(chunk) async for chunk in client._mock_stream({})]

    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert chunk["choices"][0]["finish_reason"] is None
        assert chunk["choices"][0]["delta"]["content"]
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["choices"][0]["delta"] == {}


@pytest.mark.asyncio
async def test_stream_chat_uses_mock_stream_in_debug_mode_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.services.llm_client.settings.DEBUG", True)
    client = LLMClient()
    client.api_key = ""

    chunks = [chunk async for chunk in client.stream_chat({"model": "gpt-4o-mini"})]

    assert len(chunks) > 0
    assert json.loads(chunks[-1])["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_stream_chat_calls_gateway_with_auth_header_and_request_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.services.llm_client.settings.DEBUG", False)
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeStreamResponse(["data: [DONE]"])

    client = LLMClient()
    client.gateway_url = "https://gateway.example"
    client.api_key = "secret-key"

    request = {"model": "gpt-4o-mini", "messages": []}
    _ = [chunk async for chunk in client.stream_chat(request)]

    call = FakeAsyncClient.last_instance.stream_calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://gateway.example/v1/chat/completions"
    assert call["json"] == request
    assert call["headers"]["Authorization"] == "Bearer secret-key"
    assert call["headers"]["Accept"] == "text/event-stream"


@pytest.mark.asyncio
async def test_stream_chat_parses_data_lines_and_stops_at_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.services.llm_client.settings.DEBUG", False)
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeStreamResponse(
        [
            "",
            "data: {\"choices\": [{\"delta\": {\"content\": \"Hi\"}}]}",
            "data: {\"choices\": [{\"delta\": {\"content\": \" there\"}}]}",
            "data: [DONE]",
            "data: {\"choices\": [{\"delta\": {\"content\": \"ignored after done\"}}]}",
        ]
    )

    client = LLMClient()
    client.api_key = "secret-key"

    chunks = [chunk async for chunk in client.stream_chat({"model": "gpt-4o-mini"})]

    assert chunks == [
        '{"choices": [{"delta": {"content": "Hi"}}]}',
        '{"choices": [{"delta": {"content": " there"}}]}',
    ]


@pytest.mark.asyncio
async def test_stream_chat_propagates_gateway_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.services.llm_client.settings.DEBUG", False)
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeStreamResponse(
        [], status_error=httpx.HTTPStatusError("boom", request=None, response=None)
    )

    client = LLMClient()
    client.api_key = "secret-key"

    with pytest.raises(httpx.HTTPStatusError):
        _ = [chunk async for chunk in client.stream_chat({"model": "gpt-4o-mini"})]


class _UnreadStream(httpx.AsyncByteStream):
    """A body httpx has not consumed yet — the state a real streamed error
    response is in. Needed because httpx.Response(..., json=...) arrives
    pre-read and would hide the very bug under test."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self) -> AsyncGenerator[bytes, None]:
        yield self._data


def _gateway_returning(status: int, body: bytes) -> Any:
    """A real httpx.AsyncClient wired to a MockTransport — deliberately not
    the hand-rolled FakeAsyncClient above. The bug this guards against lives
    entirely in httpx's streamed-response semantics (unread body, .text
    raising ResponseNotRead), which a fake only reproduces if it was written
    already knowing about them; this one was not, which is why the failure
    shipped."""
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, stream=_UnreadStream(body))

    def make_client(**kwargs: Any) -> Any:
        kwargs.pop("verify", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    return make_client


@pytest.mark.asyncio
async def test_stream_chat_reads_error_body_so_callers_can_report_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: picking a non-chat model (an embedding model the
    gateway lists under /v1/models) made the gateway answer 404, and the user
    got a blank assistant message with nothing in any log.

    stream_chat raised HTTPStatusError while the body was still unread, so
    `exc.response.text` in routers/chat.py's except block raised
    ResponseNotRead — a second exception, thrown from the handler that was
    supposed to report the first. It killed the handler before both the
    logger.error call and the `{"error": ...}` SSE chunk, the stream ended
    cleanly with zero deltas, and conversation-runner stored that as an empty
    completion.

    Asserting on `.text` rather than just on the raise is the whole point: the
    old code raised correctly too.
    """
    monkeypatch.setattr("src.services.llm_client.settings.DEBUG", False)
    monkeypatch.setattr(
        "httpx.AsyncClient",
        _gateway_returning(404, b'{"error":{"message":"Resource not found"}}'),
    )

    client = LLMClient()
    client.api_key = "secret-key"

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        _ = [chunk async for chunk in client.stream_chat({"model": "openai/text-embedding-3-small"})]

    assert "Resource not found" in excinfo.value.response.text


@pytest.mark.asyncio
async def test_stream_chat_does_not_read_the_body_of_a_successful_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-read is gated on is_error for a reason: buffering a *success*
    would defeat streaming entirely, turning every chat reply into one
    delayed lump. Two data lines arriving as separate chunks assert the
    happy path still streams incrementally."""
    monkeypatch.setattr("src.services.llm_client.settings.DEBUG", False)
    body = b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'
    monkeypatch.setattr("httpx.AsyncClient", _gateway_returning(200, body))

    client = LLMClient()
    client.api_key = "secret-key"

    chunks = [chunk async for chunk in client.stream_chat({"model": "gpt-4o-mini"})]

    assert chunks == ['{"choices":[{"delta":{"content":"Hi"}}]}']


@pytest.mark.asyncio
async def test_stream_chat_calls_accepted_once_before_the_first_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.services.llm_client.settings.DEBUG", False)
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeStreamResponse(
        [
            'data: {"choices":[{"delta":{"content":"Hi"}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":1,'
            '"completion_tokens":2,"total_tokens":3}}',
            "data: [DONE]",
        ]
    )
    accepted_calls = 0

    async def on_accepted() -> None:
        nonlocal accepted_calls
        accepted_calls += 1

    client = LLMClient()
    client.api_key = "secret-key"
    stream = client.stream_chat({"model": "gpt-test"}, on_accepted=on_accepted)

    first_chunk = await anext(stream)
    assert accepted_calls == 1
    remaining = [chunk async for chunk in stream]

    assert first_chunk == '{"choices":[{"delta":{"content":"Hi"}}]}'
    assert remaining == [
        '{"choices":[],"usage":{"prompt_tokens":1,'
        '"completion_tokens":2,"total_tokens":3}}'
    ]
    assert accepted_calls == 1


@pytest.mark.asyncio
async def test_stream_chat_does_not_accept_a_gateway_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.services.llm_client.settings.DEBUG", False)
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeStreamResponse(
        [], status_error=httpx.HTTPStatusError("boom", request=None, response=None)
    )
    accepted_calls = 0

    async def on_accepted() -> None:
        nonlocal accepted_calls
        accepted_calls += 1

    client = LLMClient()
    client.api_key = "secret-key"

    with pytest.raises(httpx.HTTPStatusError):
        _ = [
            chunk
            async for chunk in client.stream_chat(
                {"model": "gpt-test"}, on_accepted=on_accepted
            )
        ]

    assert accepted_calls == 0


@pytest.mark.asyncio
async def test_stream_chat_aborts_when_acceptance_persistence_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.services.llm_client.settings.DEBUG", False)
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeStreamResponse(
        ['data: {"choices":[{"delta":{"content":"must not escape"}}]}']
    )

    async def on_accepted() -> None:
        raise RuntimeError("quota persistence failed")

    client = LLMClient()
    client.api_key = "secret-key"

    with pytest.raises(RuntimeError, match="quota persistence failed"):
        _ = [
            chunk
            async for chunk in client.stream_chat(
                {"model": "gpt-test"}, on_accepted=on_accepted
            )
        ]


@pytest.mark.asyncio
async def test_debug_mock_does_not_report_upstream_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.services.llm_client.settings.DEBUG", True)
    accepted_calls = 0

    async def on_accepted() -> None:
        nonlocal accepted_calls
        accepted_calls += 1

    client = LLMClient()
    client.api_key = ""

    chunks = [
        chunk
        async for chunk in client.stream_chat(
            {"model": "gpt-test"}, on_accepted=on_accepted
        )
    ]

    assert chunks
    assert accepted_calls == 0
