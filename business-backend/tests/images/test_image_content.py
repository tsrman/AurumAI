import base64
import struct
import zlib

import pytest

from src.services.image_content import ImageContentError, inspect_image


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00" + b"\x00\x00\x00" * width)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


def _jpeg(width: int, height: int) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"JF" + sof + b"\xff\xd9"


def _webp_vp8x(width: int, height: int) -> bytes:
    payload = b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    chunk = b"VP8X" + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def test_png_dimensions_and_type() -> None:
    info = inspect_image(_png(3, 2), max_bytes=10_000)
    assert (info.content_type, info.width, info.height) == ("image/png", 3, 2)
    assert info.extension == "png"


def test_jpeg_dimensions_and_type() -> None:
    info = inspect_image(_jpeg(640, 480), max_bytes=10_000)
    assert (info.content_type, info.width, info.height) == ("image/jpeg", 640, 480)


def test_webp_dimensions_and_type() -> None:
    info = inspect_image(_webp_vp8x(1024, 768), max_bytes=10_000)
    assert (info.content_type, info.width, info.height) == ("image/webp", 1024, 768)


@pytest.mark.parametrize(
    "data",
    [
        b"<svg xmlns='http://www.w3.org/2000/svg'/>",
        b"GIF89a\x01\x00\x01\x00",
        b"",
        b"\x89PNG\r\n\x1a\n",  # truncated header
    ],
)
def test_rejects_unsupported_or_malformed_bytes(data: bytes) -> None:
    with pytest.raises(ImageContentError):
        inspect_image(data, max_bytes=10_000)


def test_rejects_oversized_payloads_and_dimensions() -> None:
    with pytest.raises(ImageContentError):
        inspect_image(_png(3, 2), max_bytes=10)
    with pytest.raises(ImageContentError):
        inspect_image(_png(20_000, 2), max_bytes=10_000)


def test_decodes_strict_base64() -> None:
    from src.services.image_content import decode_base64_image

    raw = _png(1, 1)
    assert decode_base64_image(base64.b64encode(raw).decode(), max_bytes=10_000) == raw
    with pytest.raises(ImageContentError):
        decode_base64_image("not base64!", max_bytes=10_000)
