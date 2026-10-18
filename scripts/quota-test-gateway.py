#!/usr/bin/env python3
"""Deterministic OpenAI-compatible gateway used only by quota E2E tests."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

MODEL_ID = "quota-e2e-model"
UPSTREAM_ERROR_SENTINEL = "quota-e2e-upstream-error"
NO_USAGE_SENTINEL = "quota-e2e-no-usage"
USAGE = {
    "prompt_tokens": 7,
    "completion_tokens": 5,
    "total_tokens": 12,
}


class Message(BaseModel):
    role: str
    content: str | None = None


class CompletionRequest(BaseModel):
    model: str
    messages: list[Message]
    stream: bool = True


app = FastAPI(title="Quota E2E deterministic gateway")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "owned_by": "quota-e2e"}],
    }


def _chunk(delta: dict[str, str], finish_reason: str | None = None) -> dict[str, Any]:
    return {
        "id": "quota-e2e-completion",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


async def _stream(*, include_usage: bool = True) -> AsyncIterator[str]:
    frames = [
        _chunk({"role": "assistant", "content": "quota e2e ok"}),
        _chunk({}, "stop"),
    ]
    if include_usage:
        frames.append({"choices": [], "usage": USAGE})
    for frame in frames:
        yield f"data: {json.dumps(frame, separators=(',', ':'))}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: CompletionRequest) -> Any:
    if any(
        message.content == UPSTREAM_ERROR_SENTINEL for message in request.messages
    ):
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "deterministic upstream failure"}},
        )
    include_usage = not any(
        message.content == NO_USAGE_SENTINEL for message in request.messages
    )
    return StreamingResponse(
        _stream(include_usage=include_usage),
        media_type="text/event-stream",
    )


if __name__ == "__main__":
    port = int(os.environ.get("QUOTA_E2E_GATEWAY_PORT", "18080"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
