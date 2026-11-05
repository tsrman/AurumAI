"""Whether a model answers with native tool calls, which Hermes Agent needs to use its tools.

One tiny request that forces a call to a dummy function. It is an administrator's
diagnostic when choosing an agent's own model, not a user turn: it is not billed to
a conversation and only reads the first chunks of the answer.
"""

import json
import logging
from typing import Any

from src.services.llm_client import llm_client

logger = logging.getLogger(__name__)

_REQUEST_TOOL = {
    "type": "function",
    "function": {
        "name": "ping",
        "description": "Reply by calling this function.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _has_tool_call(chunk: str) -> bool:
    try:
        payload = json.loads(chunk)
    except ValueError:
        return False
    for choice in payload.get("choices", []) if isinstance(payload, dict) else []:
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if isinstance(delta, dict) and delta.get("tool_calls"):
            return True
    return False


async def supports_tool_calls(model: str) -> bool | None:
    """True or False for the model's answer; None when the gateway could not answer."""
    request: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Call the ping function."}],
        "tools": [_REQUEST_TOOL],
        "tool_choice": {"type": "function", "function": {"name": "ping"}},
        "max_tokens": 32,
        "stream": True,
    }
    try:
        async for chunk in llm_client.stream_chat(request):
            if _has_tool_call(chunk):
                return True
    except Exception:
        logger.warning("Tool-call probe of %s failed", model, exc_info=True)
        return None
    return False
