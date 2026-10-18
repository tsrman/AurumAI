"""Bounded adapter for the operator-owned SearXNG JSON API."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re
import time
from typing import Any, Literal
import unicodedata
from urllib.parse import urlsplit

import aiohttp

from src.policy import URLBlocked
from src.transport import (
    DownloadBuffer,
    FetchFailed,
    Resolver,
    resolve_target,
    system_resolver,
)


MAX_SEARXNG_RESPONSE_BYTES = 1024 * 1024
MAX_RESULTS = 5
MAX_RESULT_CANDIDATES = 20
TimeRange = Literal["day", "week", "month", "year"]
_LANGUAGE_OVERRIDE = re.compile(r"(?<!\S):\S*(?=\s|$)")
_TIMEOUT_OVERRIDE = re.compile(r"(?<!\S)<\d+(?=\s|$)")


class SearchFailed(ValueError):
    def __init__(self, code: str = "web_unavailable", *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    published_at: datetime | None


def validate_search_query(query: str) -> str:
    if (
        not query
        or query != query.strip()
        or len(query) > 2000
        or any(unicodedata.category(character) in {"Cc", "Cf"} for character in query)
        # SearXNG treats !engine, !category, !!external-bang and !! lucky
        # as routing controls. Rejecting every exclamation mark is deliberate:
        # upstream permits query parts beyond the first token.
        or "!" in query
        or _LANGUAGE_OVERRIDE.search(query)
        # RawTextQuery also accepts a per-query transport timeout such as
        # <3 or <850; the operator owns that deadline.
        or _TIMEOUT_OVERRIDE.search(query)
    ):
        raise ValueError("invalid_search_query")
    return query


def _published_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _endpoint(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("invalid SearXNG endpoint") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("invalid SearXNG endpoint")
    return value.rstrip("/") + "/search"


class SearxngAdapter:
    def __init__(
        self,
        *,
        endpoint: str,
        result_resolver: Resolver = system_resolver,
        forbidden_cidrs: tuple[str, ...] = (),
        deadline_seconds: float = 30,
        max_attempts: int = 2,
    ) -> None:
        self._endpoint = _endpoint(endpoint)
        self._result_resolver = result_resolver
        self._forbidden_cidrs = forbidden_cidrs
        self._deadline_seconds = deadline_seconds
        self._max_attempts = max_attempts

    async def search(
        self,
        query: str,
        language: str | None,
        time_range: TimeRange | None,
    ) -> list[SearchHit]:
        payload = {"q": validate_search_query(query), "format": "json"}
        if language is not None:
            payload["language"] = language
        if time_range is not None:
            payload["time_range"] = time_range
        deadline = time.monotonic() + self._deadline_seconds
        document: dict[str, Any] | None = None
        for attempt in range(self._max_attempts):
            try:
                document = await self._request(payload, deadline - time.monotonic())
                break
            except SearchFailed as exc:
                if not exc.retryable or attempt + 1 >= self._max_attempts:
                    raise
                if time.monotonic() >= deadline:
                    raise SearchFailed("web_timeout") from exc
        if document is None:
            raise SearchFailed()
        return await self._normalize(document, deadline)

    async def _request(self, payload: dict[str, str], remaining: float) -> dict[str, Any]:
        if remaining <= 0:
            raise SearchFailed("web_timeout")
        timeout = aiohttp.ClientTimeout(total=remaining, connect=min(10.0, remaining))
        try:
            async with aiohttp.ClientSession(
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),
                auto_decompress=False,
                headers={
                    "User-Agent": "aurum-ai-web-tools/1",
                    "Accept": "application/json",
                },
            ) as client:
                async with client.post(
                    self._endpoint,
                    data=payload,
                    allow_redirects=False,
                    timeout=timeout,
                ) as response:
                    if response.status == 429 or 300 <= response.status < 500:
                        raise SearchFailed()
                    if response.status >= 500:
                        raise SearchFailed(retryable=True)
                    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                    if content_type != "application/json" and not content_type.endswith("+json"):
                        raise SearchFailed()
                    if (
                        response.content_length is not None
                        and response.content_length > MAX_SEARXNG_RESPONSE_BYTES
                    ):
                        raise SearchFailed()
                    buffer = DownloadBuffer(
                        content_encoding=response.headers.get("Content-Encoding", "identity"),
                        max_bytes=MAX_SEARXNG_RESPONSE_BYTES,
                    )
                    async for chunk in response.content.iter_chunked(65536):
                        buffer.consume(chunk)
                    try:
                        value = json.loads(buffer.finish())
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise SearchFailed() from exc
        except FetchFailed as exc:
            raise SearchFailed() from exc
        except asyncio.TimeoutError as exc:
            raise SearchFailed("web_timeout", retryable=True) from exc
        except aiohttp.ClientError as exc:
            raise SearchFailed(retryable=True) from exc
        if not isinstance(value, dict):
            raise SearchFailed()
        return value

    async def _normalize(self, document: dict[str, Any], deadline: float) -> list[SearchHit]:
        raw_results = document.get("results")
        if not isinstance(raw_results, list):
            raise SearchFailed()
        if not raw_results and document.get("unresponsive_engines"):
            raise SearchFailed()
        normalized: list[SearchHit] = []
        for raw in raw_results[:MAX_RESULT_CANDIDATES]:
            if len(normalized) >= MAX_RESULTS:
                break
            if not isinstance(raw, dict):
                continue
            title, url = raw.get("title"), raw.get("url")
            snippet = raw.get("content", "")
            if (
                not isinstance(title, str)
                or not title.strip()
                or not isinstance(url, str)
                or not isinstance(snippet, str)
            ):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SearchFailed("web_timeout")
            try:
                async with asyncio.timeout(remaining):
                    target = await resolve_target(
                        url,
                        resolver=self._result_resolver,
                        forbidden_cidrs=self._forbidden_cidrs,
                    )
            except TimeoutError as exc:
                raise SearchFailed("web_timeout") from exc
            except (URLBlocked, FetchFailed):
                continue
            normalized.append(
                SearchHit(
                    title=title[:2000],
                    url=target.url,
                    snippet=snippet[:4000],
                    published_at=_published_at(
                        raw.get("publishedDate", raw.get("published_date"))
                    ),
                )
            )
        return normalized
