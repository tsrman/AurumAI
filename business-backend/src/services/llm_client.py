import json
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, AsyncGenerator, Dict
from src.core.config import settings


class LLMClient:
    def __init__(self) -> None:
        self.gateway_url = settings.LLM_GATEWAY_URL
        self.api_key = settings.GATEWAY_API_KEY

    async def stream_chat(
        self,
        request: Dict[str, Any],
        *,
        on_accepted: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncGenerator[str, None]:
        if settings.DEBUG and not self.api_key:
            async for chunk in self._mock_stream(request):
                yield chunk
            return

        import httpx
        async with httpx.AsyncClient(
            timeout=120.0, verify=settings.gateway_verify
        ) as client:
            async with client.stream(
                "POST",
                f"{self.gateway_url}/v1/chat/completions",
                json=request,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
            ) as response:
                if response.is_error:
                    # Pull the body in before raising: on a streamed response
                    # httpx leaves it unread, so `exc.response.text` in the
                    # caller's except block raises ResponseNotRead instead of
                    # returning the gateway's error. That secondary exception
                    # killed chat_completions' handler (routers/chat.py) before
                    # it could log anything or yield its `{"error": ...}` chunk,
                    # so the SSE stream just ended — and conversation-runner,
                    # seeing a clean stream with no deltas, stored an *empty
                    # assistant message*. Every gateway failure (404 for a
                    # non-chat model, 401, 429, 5xx) surfaced in the UI as a
                    # blank reply with no error anywhere in the logs.
                    await response.aread()
                response.raise_for_status()
                if on_accepted is not None:
                    await on_accepted()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        yield data

    async def _mock_stream(
        self,
        request: Dict[str, Any],
    ) -> AsyncGenerator[str, None]:
        text = "This is a test streaming response from the mock LLM. " * 5
        words = text.split()
        for word in words:
            chunk = {
                "id": "mock-chunk",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": word + " "},
                        "finish_reason": None,
                    }
                ],
            }
            yield json.dumps(chunk)
            await asyncio.sleep(0.05)

        final = {
            "id": "mock-final",
            "object": "chat.completion.chunk",
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}
            ],
        }
        yield json.dumps(final)


llm_client = LLMClient()
