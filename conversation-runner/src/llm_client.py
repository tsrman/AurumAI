import httpx
from collections.abc import AsyncIterator
from typing import Any

from temporalio.exceptions import ApplicationError

from src.config import settings


def _error_body(response: httpx.Response) -> str:
    """The response body if it's available, "" otherwise. On a streamed
    response httpx leaves the body unread and `.text` raises ResponseNotRead,
    so this must never be called without a guard — the whole point is that a
    diagnostic helper can't be allowed to throw a second exception over the
    first one (that is exactly what hid every gateway failure behind an empty
    assistant message; see services/llm_client.py in business-backend)."""
    try:
        return response.text[:2000]
    except Exception:
        return ""


def raise_non_retryable_on_4xx(response: httpx.Response) -> None:
    """`response.raise_for_status()`, but a 4xx (bad request, expired/
    revoked session token, deleted conversation, ...) is re-raised as a
    non-retryable ApplicationError — otherwise, with the default (infinite)
    Temporal retry policy, a persistent 4xx retries forever and every
    subsequent turn hangs on the exact same error.
    A 5xx still raises the plain httpx error, which stays retryable.

    The body is appended to the message because httpx's own string carries
    only the status and URL — for a gateway 404 that's "Client error '404 Not
    Found'" with no hint that the *model* was the problem."""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if 400 <= exc.response.status_code < 500:
            body = _error_body(exc.response)
            message = f"{exc} — {body}" if body else str(exc)
            raise ApplicationError(message, non_retryable=True) from exc
        raise


class LLMClient:
    async def stream_chat(self, request: dict[str, Any]) -> AsyncIterator[str]:
        # Read settings fresh on every call rather than snapshotting at
        # construction time — in pool mode the container starts with no
        # session_token/gateway_url until /bind assigns it a conversation,
        # well after this module (and its module-level singleton below) is
        # imported.
        headers = {"Authorization": f"Bearer {settings.session_token}"}
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream(
                "POST",
                f"{settings.llm_gateway_url}/v1/chat/completions",
                json=request,
                headers=headers,
            ) as response:
                # Read the streamed body before raising so _error_body can
                # actually report why the gateway refused the call.
                if response.is_error:
                    await response.aread()
                raise_non_retryable_on_4xx(response)
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        yield line[6:]


llm_client = LLMClient()
