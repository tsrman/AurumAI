from __future__ import annotations

import hashlib
import logging
from typing import Any

from langfuse import Langfuse

try:
    from langfuse.client import StatefulTraceClient
except ImportError:
    # Fallback if the internal client module is unavailable
    StatefulTraceClient = Any

from src.config import settings

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
        try:
            _langfuse = Langfuse(
                public_key=settings.LANGFUSE_PUBLIC_KEY,
                secret_key=settings.LANGFUSE_SECRET_KEY,
                host=settings.LANGFUSE_HOST,
                flush_interval=settings.LANGFUSE_FLUSH_INTERVAL,
            )
        except Exception as exc:
            logger.warning("Langfuse client creation failed: %s", exc)
            return None
    return _langfuse


def shutdown_langfuse() -> None:
    global _langfuse
    if _langfuse is not None:
        try:
            _langfuse.shutdown()
        except Exception as exc:
            logger.warning("Langfuse shutdown failed: %s", exc)
        finally:
            _langfuse = None


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_content(value)
    if isinstance(value, dict):
        return {k: _sanitize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(v) for v in value]
    return value


def sanitize_args(args: dict[str, Any] | None) -> dict[str, Any] | None:
    if args is None:
        return None
    return {k: _sanitize_value(v) for k, v in args.items()}


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


def get_or_create_trace(name: str, **kwargs: Any) -> StatefulTraceClient | None:
    lf = get_langfuse()
    if lf is None:
        return None
    try:
        return lf.trace(name=name, **kwargs)
    except Exception as exc:
        logger.warning("Langfuse trace creation failed: %s", exc)
        return None
