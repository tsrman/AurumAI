"""Gateway messages for Hermes native-tools completions.

Inline images are decoded and inspected exactly like uploads. As on the runner
route, only the newest MAX_VISION_IMAGES_PER_REQUEST images reach a vision
model; older ones, or all of them for a model the organization did not mark as
vision, are replaced by OMITTED_IMAGE_NOTE.
"""

import base64
import binascii
from collections.abc import Sequence
from typing import Any

from src.core.config import settings
from src.schemas.hermes import HermesImagePart, HermesMessage, HermesTextPart
from src.services.image_content import ImageContentError, inspect_image
from src.services.image_uploads import MAX_VISION_IMAGES_PER_REQUEST, OMITTED_IMAGE_NOTE


class HermesRequestError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _checked_image(part: HermesImagePart) -> None:
    header, _, encoded = part.image_url.url.partition(",")
    declared = header.removeprefix("data:").removesuffix(";base64")
    try:
        data = base64.b64decode(encoded, validate=True)
        info = inspect_image(data, max_bytes=settings.IMAGES_UPLOAD_MAX_BYTES)
    except (binascii.Error, ValueError, ImageContentError):
        raise HermesRequestError("hermes_invalid_image") from None
    if info.content_type != declared:
        raise HermesRequestError("hermes_invalid_image")


def gateway_messages(messages: Sequence[HermesMessage], *, vision: bool) -> list[dict[str, Any]]:
    keep: set[tuple[int, int]] = set()
    remaining = MAX_VISION_IMAGES_PER_REQUEST if vision else 0
    for index in range(len(messages) - 1, -1, -1):
        content = messages[index].content
        if not isinstance(content, list):
            continue
        for position in range(len(content) - 1, -1, -1):
            part = content[position]
            if isinstance(part, HermesImagePart):
                _checked_image(part)
                if remaining > 0:
                    keep.add((index, position))
                    remaining -= 1
    result: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        entry: dict[str, Any] = {"role": message.role, "content": message.content}
        if isinstance(message.content, list):
            parts: list[dict[str, Any]] = []
            omitted = False
            for position, part in enumerate(message.content):
                if isinstance(part, HermesTextPart) or (index, position) in keep:
                    parts.append(part.model_dump(exclude_none=True))
                else:
                    omitted = True
            if omitted:
                parts.append({"type": "text", "text": OMITTED_IMAGE_NOTE})
            entry["content"] = parts
        if message.name is not None:
            entry["name"] = message.name
        if message.tool_calls:
            entry["tool_calls"] = [call.model_dump() for call in message.tool_calls]
        if message.tool_call_id is not None:
            entry["tool_call_id"] = message.tool_call_id
        result.append(entry)
    return result


def has_images(messages: Sequence[HermesMessage]) -> bool:
    return any(
        isinstance(part, HermesImagePart)
        for message in messages
        if isinstance(message.content, list)
        for part in message.content
    )
