import asyncio
import time

import pytest

from src.parser_sandbox import ParserFailed
from src.reader import PublicReader
from src.transport import Download


class _Transport:
    def __init__(self, download: Download, delay: float = 0) -> None:
        self._download = download
        self._delay = delay
        self.deadlines: list[float] = []

    async def fetch(self, url: str, *, deadline_seconds: float = 30) -> Download:
        assert url == "https://example.org/report"
        self.deadlines.append(deadline_seconds)
        await asyncio.sleep(self._delay)
        return self._download


async def test_public_reader_returns_final_provenance_and_extracted_text() -> None:
    transport = _Transport(
        Download(
            final_url="https://www.example.org/final",
            content_type="text/html",
            body=b"<title>Report</title><p>Verified content</p>",
        )
    )

    result = await PublicReader(transport=transport).read("https://example.org/report")

    assert result.requested_url == "https://example.org/report"
    assert result.final_url == "https://www.example.org/final"
    assert result.content_type == "text/html"
    assert result.title == "Report"
    assert result.text == "Verified content"
    assert result.truncated is False
    assert result.page_count is None
    assert len(transport.deadlines) == 1
    assert 0 < transport.deadlines[0] <= 30


async def test_fetch_and_parse_share_one_deadline() -> None:
    transport = _Transport(
        Download(
            final_url="https://example.org/report",
            content_type="text/html",
            body=b"<p>content</p>",
        ),
        delay=0.02,
    )

    started = time.monotonic()
    with pytest.raises(ParserFailed, match="web_timeout"):
        await PublicReader(transport=transport, deadline_seconds=0.021).read(
            "https://example.org/report"
        )
    assert time.monotonic() - started < 0.04
