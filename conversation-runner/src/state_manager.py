from typing import Any
from urllib.parse import urlencode

import httpx

from src.config import settings


class StateManager:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        # Reserved for future agent shared context.
        self.context: dict[str, Any] = {}

    async def load(self) -> None:
        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        async with httpx.AsyncClient() as client:
            while True:
                url = f"{settings.llm_gateway_url}/v1/conversations/{settings.conversation_id}"
                if cursor is not None:
                    url += "?" + urlencode({"cursor": cursor})
                response = await client.get(
                    url, headers={"Authorization": f"Bearer {settings.session_token}"},
                )
                response.raise_for_status()
                try:
                    page = response.json()["data"]["messages"]
                    messages.extend(
                        {"role": m["role"], "content": m["content"]}
                        | ({"turn_id": m["turnId"]} if m.get("turnId") else {})
                        for m in page["data"]
                    )
                except KeyError as exc:
                    raise ValueError(
                        f"Unexpected conversation API response shape: {exc}"
                    ) from exc
                pagination = page.get("pagination", {})
                if not pagination.get("hasMore", False):
                    break
                next_cursor = pagination.get("cursor")
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                    raise ValueError("Invalid message history cursor")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
        # An HTTP/contract failure on a later page must not leave partial state.
        self.messages = messages

    def append(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


state_manager = StateManager()
