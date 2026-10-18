"""One-deadline public fetch and isolated extraction composition."""

import asyncio
from dataclasses import dataclass
import time
from typing import Protocol

from src.parser_sandbox import ParserFailed, parse_isolated
from src.transport import Download, PinnedTransport


class Fetcher(Protocol):
    async def fetch(self, url: str, *, deadline_seconds: float = 30) -> Download: ...


@dataclass(frozen=True, slots=True)
class ReadResult:
    requested_url: str
    final_url: str
    content_type: str
    text: str
    title: str | None
    truncated: bool
    page_count: int | None


class PublicReader:
    def __init__(
        self,
        *,
        transport: Fetcher | None = None,
        deadline_seconds: float = 30,
    ) -> None:
        self._transport = transport or PinnedTransport()
        self._deadline_seconds = deadline_seconds

    async def read(self, url: str) -> ReadResult:
        if self._deadline_seconds <= 0:
            raise ParserFailed("web_timeout")
        deadline = time.monotonic() + self._deadline_seconds
        try:
            async with asyncio.timeout(self._deadline_seconds):
                download = await self._transport.fetch(
                    url,
                    deadline_seconds=deadline - time.monotonic(),
                )
                extracted = await parse_isolated(download, deadline - time.monotonic())
        except TimeoutError as exc:
            raise ParserFailed("web_timeout") from exc
        return ReadResult(
            requested_url=url,
            final_url=download.final_url,
            content_type=download.content_type,
            text=extracted.text,
            title=extracted.title,
            truncated=extracted.truncated,
            page_count=extracted.page_count,
        )
