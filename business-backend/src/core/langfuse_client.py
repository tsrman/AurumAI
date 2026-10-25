from __future__ import annotations

import hashlib
import logging
from typing import Any

from langfuse import Langfuse

from src.core.config import settings

logger = logging.getLogger(__name__)

_langfuse: Langfuse | None = None


def get_langfuse() -> Langfuse | None:
    global _langfuse
    if not settings.LANGFUSE_ENABLED:
        return None
    if _langfuse is None:
        if not settings.LANGFUSE_PUBLIC_KEY or not settings.LANGFUSE_SECRET_KEY:
            logger.warning("Langfuse enabled but keys missing")
            return None
        _langfuse = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST,
            flush_interval=settings.LANGFUSE_FLUSH_INTERVAL,
        )
    return _langfuse


def sanitize_content(text: str | None) -> str | None:
    if settings.LANGFUSE_CAPTURE_FULL_CONTENT:
        return text
    if text is None:
        return None
    return f"<redacted len={len(text)} sha={hashlib.sha256(text.encode()).hexdigest()[:16]}>"


def sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "role": m.get("role"),
            "content": sanitize_content(m.get("content")),
        }
        for m in messages
    ]


def _sanitize_part(part: Any) -> Any:
    if not isinstance(part, dict):
        return None
    if part.get("type") == "text":
        return {"type": "text", "text": sanitize_content(part.get("text"))}
    url = str((part.get("image_url") or {}).get("url") or "")
    # Image bytes never go to the trace, even in full-capture mode.
    mime = url.partition(";")[0].removeprefix("data:")
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    return {"type": "image_url", "image_url": f"<image {mime} len={len(url)} sha={digest}>"}


def sanitize_native_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Trace view of OpenAI messages with content parts and native tool calls."""
    result: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        entry: dict[str, Any] = {
            "role": message.get("role"),
            "content": [_sanitize_part(part) for part in content]
            if isinstance(content, list)
            else sanitize_content(content),
        }
        if message.get("tool_calls"):
            entry["tool_calls"] = [
                {
                    "id": call.get("id"),
                    "name": (call.get("function") or {}).get("name"),
                    "arguments": sanitize_content((call.get("function") or {}).get("arguments")),
                }
                for call in message["tool_calls"]
            ]
        if message.get("tool_call_id"):
            entry["tool_call_id"] = message["tool_call_id"]
        result.append(entry)
    return result
