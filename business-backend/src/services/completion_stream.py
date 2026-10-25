"""Proxy one streamed chat completion from the gateway with quota accounting.

Both completion routes (the runner's text-protocol route and Hermes's native
tools route) forward the gateway's SSE chunks unchanged and share the same
accept/complete/cancel lifecycle of the request's quota reservation.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import status

from src.core.exceptions import PlatformException
from src.core.langfuse_client import sanitize_content
from src.services.llm_client import llm_client
from src.services.quota import QuotaExceededError, QuotaService


def quota_exceeded_exception(exc: QuotaExceededError) -> PlatformException:
    return PlatformException(
        code="quota_exceeded",
        message="Monthly quota exceeded",
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        details={
            "resource": exc.resource,
            "used": exc.used,
            "limit": exc.limit,
            "periodEnd": exc.period_end.isoformat(),
        },
    )


def parse_provider_usage(
    chunk_json: str,
    *,
    request_id: uuid.UUID,
    org_id: uuid.UUID,
    model: str,
    log: logging.Logger,
) -> tuple[int, int, int] | None:
    try:
        payload = json.loads(chunk_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or "usage" not in payload:
        return None
    usage = payload["usage"]
    if usage is None:
        # Not a defect: with stream_options {"include_usage": true} an
        # OpenAI-compatible provider puts `"usage": null` on *every* chunk and
        # the real figures only on the last one. Warning here logged a line
        # per token of every stream.
        return None
    values: tuple[object, object, object] | None = None
    if isinstance(usage, dict):
        values = (
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            usage.get("total_tokens"),
        )
    if values is None or any(type(value) is not int or value < 0 for value in values):
        log.warning(
            "invalid LLM usage ignored (request_id=%s org_id=%s model=%s)",
            request_id,
            org_id,
            model,
        )
        return None
    return values  # type: ignore[return-value]


@dataclass
class CompletionOutput:
    """Text and native tool calls accumulated from the stream for tracing."""

    content: str = ""
    tool_calls: dict[int, dict[str, str]] = field(default_factory=dict)

    def add(self, chunk_json: str) -> None:
        try:
            delta = json.loads(chunk_json)["choices"][0].get("delta", {})
        except Exception:
            return
        if not isinstance(delta, dict):
            return
        content = delta.get("content")
        if isinstance(content, str):
            self.content += content
        calls = delta.get("tool_calls")
        if not isinstance(calls, list):
            return
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("index"), int):
                continue
            entry = self.tool_calls.setdefault(call["index"], {"name": "", "arguments": ""})
            function = call.get("function")
            if isinstance(function, dict):
                if isinstance(function.get("name"), str):
                    entry["name"] += function["name"]
                if isinstance(function.get("arguments"), str):
                    entry["arguments"] += function["arguments"]

    def traced(self) -> dict[str, Any]:
        output: dict[str, Any] = {"completion": sanitize_content(self.content)}
        if self.tool_calls:
            output["tool_calls"] = [
                {"name": call["name"], "arguments": sanitize_content(call["arguments"])}
                for _, call in sorted(self.tool_calls.items())
            ]
        return output


async def proxy_completion_events(
    llm_request: dict[str, Any],
    *,
    quota: QuotaService,
    org_id: uuid.UUID,
    request_id: uuid.UUID,
    model: str,
    generation: Any,
    log: logging.Logger,
) -> AsyncGenerator[str, None]:
    output = CompletionOutput()
    provider_accepted = False
    last_usage: tuple[int, int, int] | None = None

    async def on_accepted() -> None:
        nonlocal provider_accepted
        provider_accepted = True
        await quota.accept(org_id, request_id)

    try:
        try:
            async for chunk_json in llm_client.stream_chat(llm_request, on_accepted=on_accepted):
                if chunk_json:
                    yield f"data: {chunk_json}\n\n"
                    usage = parse_provider_usage(
                        chunk_json, request_id=request_id, org_id=org_id, model=model, log=log
                    )
                    if usage is not None:
                        last_usage = usage
                    output.add(chunk_json)
        except httpx.HTTPStatusError as exc:
            # By the time the gateway's status is known, StreamingResponse
            # has already sent 200 to the caller — the real HTTP status can't
            # change now. Without this, the connection just dropped mid-stream
            # with nothing in the body and nothing in our own logs: the caller
            # sees a truncated response and treats it as an empty completion
            # instead of a real failure. Logging the gateway's actual error
            # body here is the only place that's ever visible.
            # `.text` is guarded, not just null-checked: on a streamed
            # response httpx leaves the body unread and raises
            # ResponseNotRead. That secondary exception used to kill this
            # handler before either the log line or the error chunk below —
            # so the stream ended silently and the runner recorded an empty
            # assistant message (llm_client now pre-reads the body, this is
            # the second line of defence so a diagnostic can never again
            # throw over the failure it is describing).
            try:
                body = exc.response.text if exc.response is not None else ""
            except Exception:
                body = "<unread>"
            log.error("LLM gateway error mid-stream (model=%s): %s — %s", model, exc, body[:2000])
            # The gateway's own body goes to the caller too, not just to our
            # logs: "Client error '404 Not Found'" alone never explains that
            # the selected model can't do chat completions.
            message = f"{exc} — {body[:2000]}" if body else str(exc)
            yield f"data: {json.dumps({'error': {'message': message}})}\n\n"
        else:
            if last_usage is not None:
                # Guarded exactly like the cancel() below: by this point
                # every content chunk has already been delivered, so an
                # accounting failure (a QuotaInvariantError on a conflicting
                # repeat completion, a database still down after
                # _retry_transient) must not be allowed to escape. Unguarded
                # it skipped generation.end() — leaking the Langfuse span —
                # and the `[DONE]` line below, leaving the caller with a
                # truncated transport instead of a clean end to a request that
                # actually succeeded.
                try:
                    await asyncio.wait_for(
                        asyncio.shield(quota.complete(org_id, request_id, *last_usage)),
                        timeout=3.0,
                    )
                except Exception:
                    log.error(
                        "quota completion failed, tokens unaccounted "
                        "(request_id=%s org_id=%s model=%s)",
                        request_id,
                        org_id,
                        model,
                        exc_info=True,
                    )
            elif provider_accepted:
                # The stream ended cleanly but the provider never sent a usage
                # block (a gateway that drops stream_options, a model that
                # reports nothing). The request itself stays counted — it was
                # accepted — but its tokens never will be, and that has to be
                # visible somewhere. Gated on provider_accepted so the DEBUG
                # mock stream, which by design never reaches a gateway and has
                # its reservation cancelled below, stays silent instead of
                # warning once per local request.
                log.warning(
                    "LLM stream ended without usage, tokens unaccounted "
                    "(request_id=%s org_id=%s model=%s)",
                    request_id,
                    org_id,
                    model,
                )
        if generation:
            generation.end(output=output.traced())
        yield "data: [DONE]\n\n"
    finally:
        if not provider_accepted:
            try:
                await asyncio.wait_for(
                    asyncio.shield(quota.cancel(org_id, request_id)), timeout=3.0
                )
            except Exception:
                log.warning(
                    "quota reservation cleanup failed (request_id=%s org_id=%s model=%s)",
                    request_id,
                    org_id,
                    model,
                    exc_info=True,
                )
