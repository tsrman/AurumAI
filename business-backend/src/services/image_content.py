"""Validate image bytes before they are stored or served.

Only PNG, JPEG and WebP are accepted, identified by their magic bytes rather
than by any declared type. SVG and other formats are refused outright: they
can carry script and would be served from the platform's own origin.
"""

import base64
import binascii
import struct
from dataclasses import dataclass

MAX_DIMENSION = 8192


class ImageContentError(ValueError):
    """The bytes are not an acceptable image."""


@dataclass(frozen=True, slots=True)
class ImageInfo:
    content_type: str
    extension: str
    width: int | None
    height: int | None
    size_bytes: int


def decode_base64_image(value: str, *, max_bytes: int) -> bytes:
    # Base64 inflates by 4/3; refuse obviously oversized text before decoding.
    if len(value) > (max_bytes * 4) // 3 + 8:
        raise ImageContentError("image too large")
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageContentError("invalid base64 image") from exc


def inspect_image(data: bytes, *, max_bytes: int) -> ImageInfo:
    if not data:
        raise ImageContentError("empty image")
    if len(data) > max_bytes:
        raise ImageContentError("image too large")
    width: int | None
    height: int | None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        width, height = _png_size(data)
        info = ImageInfo("image/png", "png", width, height, len(data))
    elif data.startswith(b"\xff\xd8\xff"):
        width, height = _jpeg_size(data)
        info = ImageInfo("image/jpeg", "jpg", width, height, len(data))
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        width, height = _webp_size(data)
        info = ImageInfo("image/webp", "webp", width, height, len(data))
    else:
        raise ImageContentError("unsupported image type")
    for side in (info.width, info.height):
        if side is not None and not 0 < side <= MAX_DIMENSION:
            raise ImageContentError("unsupported image dimensions")
    return info


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise ImageContentError("malformed PNG")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _jpeg_size(data: bytes) -> tuple[int | None, int | None]:
    position = 2
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            raise ImageContentError("malformed JPEG")
        marker = data[position + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            position += 2
            continue
        if marker == 0xD9:
            break
        (length,) = struct.unpack(">H", data[position + 2 : position + 4])
        if length < 2:
            raise ImageContentError("malformed JPEG")
        # SOF0..SOF15 except DHT (C4), JPG (C8) and DAC (CC) carry the size.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if position + 9 > len(data):
                raise ImageContentError("malformed JPEG")
            height, width = struct.unpack(">HH", data[position + 5 : position + 9])
            return int(width), int(height)
        position += 2 + length
    # Valid JPEG markers without a frame header: keep the image, size unknown.
    return None, None


def _webp_size(data: bytes) -> tuple[int | None, int | None]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return int(width) & 0x3FFF, int(height) & 0x3FFF
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk in (b"VP8X", b"VP8 ", b"VP8L"):
        raise ImageContentError("malformed WebP")
    raise ImageContentError("unsupported WebP")
