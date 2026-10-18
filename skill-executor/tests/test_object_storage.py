import asyncio

import pytest

from src.config import settings
from src.object_storage import ObjectStorage, ObjectStorageError


class _RawClientResponse:
    """What `async with <StreamingBody>` actually yields in production.

    aiobotocore's StreamingBody is a wrapt ObjectProxy around aiohttp's
    ClientResponse and does not define __aenter__ itself, so the call is
    forwarded to the wrapped response — which returns *itself*, not the
    proxy. ClientResponse.read() takes no size argument, hence the
    no-argument signature here: any code that reads through the yielded
    object instead of the StreamingBody blows up on the first chunked read.
    """

    def __init__(self, body: "_FakeStreamBody") -> None:
        self._body = body

    async def read(self) -> bytes:
        return await self._body.read(None)


class _FakeStreamBody:
    """Mimics aiobotocore's StreamingBody: async context manager + chunked .read(n)."""

    def __init__(self, data: bytes, chunk_size: int = 64 * 1024) -> None:
        self._data = data
        self._chunk_size = chunk_size
        self._pos = 0

    async def __aenter__(self) -> _RawClientResponse:
        return _RawClientResponse(self)

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def read(self, amt: int | None = None) -> bytes:
        size = amt if amt is not None else len(self._data) - self._pos
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


_UNSET = object()


class _FakeS3Client:
    def __init__(self, data: bytes, content_length: object = _UNSET, delay: float = 0.0) -> None:
        self._data = data
        self._content_length = len(data) if content_length is _UNSET else content_length
        self._delay = delay

    async def __aenter__(self) -> "_FakeS3Client":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get_object(self, Bucket: str, Key: str) -> dict:
        if self._delay:
            await asyncio.sleep(self._delay)
        return {"ContentLength": self._content_length, "Body": _FakeStreamBody(self._data)}


def _patch_client(monkeypatch, client: _FakeS3Client) -> None:
    storage = ObjectStorage()
    monkeypatch.setattr(storage, "_client", lambda: client)
    return storage


@pytest.mark.asyncio
async def test_get_bytes_returns_content_within_limit(monkeypatch):
    monkeypatch.setattr(settings, "bundle_max_bytes", 1024)
    storage = _patch_client(monkeypatch, _FakeS3Client(b"hello world"))

    result = await storage.get_bytes("bundles/skill.zip")

    assert result == b"hello world"


@pytest.mark.asyncio
async def test_get_bytes_reads_through_the_streaming_body_not_the_yielded_response(monkeypatch):
    """Regression test: every code-skill session failed with 502 "Could not
    fetch bundle ...: ClientResponse.read() takes 1 positional argument but 2
    were given". `async with response["Body"] as stream` rebinds `stream` to
    the raw aiohttp ClientResponse (see _RawClientResponse above), so the
    chunked `stream.read(_READ_CHUNK_BYTES)` added for the bundle size cap
    hit a read() that takes no size argument. The fix keeps a reference to
    the StreamingBody itself and only enters its context manager (which is
    what releases the connection).

    Multiple chunks here on purpose — a single-chunk payload would pass even
    if the size argument were silently dropped.
    """
    monkeypatch.setattr(settings, "bundle_max_bytes", 10 * 1024 * 1024)
    payload = bytes(range(256)) * 4096  # 1 MiB — 16 chunks at 64 KiB
    storage = _patch_client(monkeypatch, _FakeS3Client(payload))

    assert await storage.get_bytes("bundles/skill.zip") == payload


@pytest.mark.asyncio
async def test_get_bytes_rejects_when_content_length_exceeds_limit(monkeypatch):
    monkeypatch.setattr(settings, "bundle_max_bytes", 10)
    storage = _patch_client(
        monkeypatch, _FakeS3Client(b"x" * 20, content_length=20)
    )

    with pytest.raises(ObjectStorageError, match="exceeding"):
        await storage.get_bytes("bundles/skill.zip")


@pytest.mark.asyncio
async def test_get_bytes_aborts_mid_stream_if_actual_size_exceeds_limit(monkeypatch):
    # ContentLength lies (or is absent) but the real stream is oversized —
    # the streaming read must still catch it (defense
    # in depth, not just trusting S3-reported metadata).
    monkeypatch.setattr(settings, "bundle_max_bytes", 10)
    storage = _patch_client(
        monkeypatch, _FakeS3Client(b"x" * 1000, content_length=None)
    )

    with pytest.raises(ObjectStorageError, match="exceeded"):
        await storage.get_bytes("bundles/skill.zip")


@pytest.mark.asyncio
async def test_get_bytes_times_out_on_a_slow_fetch(monkeypatch):
    monkeypatch.setattr(settings, "bundle_fetch_timeout_seconds", 0.05)
    monkeypatch.setattr(settings, "bundle_max_bytes", 1024)
    storage = _patch_client(
        monkeypatch, _FakeS3Client(b"hello", delay=1.0)
    )

    with pytest.raises(ObjectStorageError, match="Timed out"):
        await storage.get_bytes("bundles/skill.zip")


@pytest.mark.asyncio
async def test_get_bytes_wraps_underlying_errors(monkeypatch):
    class _BrokenClient:
        async def __aenter__(self):
            raise RuntimeError("connection refused")

        async def __aexit__(self, *exc: object) -> bool:
            return False

    storage = _patch_client(monkeypatch, _BrokenClient())

    with pytest.raises(ObjectStorageError, match="Could not fetch bundle"):
        await storage.get_bytes("bundles/skill.zip")
