import base64
import json

import httpx
import pytest

from src.services.image_api_client import ImageApiClient, ImageApiError, ImageUsage

PNG = b"\x89PNG\r\n\x1a\nfake"


def _client(handler) -> ImageApiClient:
    return ImageApiClient(
        base_url="https://gateway.example",
        api_key="secret",
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )


async def test_generation_posts_json_and_decodes_base64() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

    result = await _client(handler).generate(model="gpt-image-2", prompt="a cat", size="1024x1024")

    assert result.data == PNG and result.usage is None
    assert seen["url"] == "https://gateway.example/v1/images/generations"
    assert seen["auth"] == "Bearer secret"
    assert seen["body"] == {"model": "gpt-image-2", "prompt": "a cat", "n": 1, "size": "1024x1024"}


async def test_edit_sends_multipart_with_the_source_image() -> None:
    seen: dict[str, bytes | str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["type"] = request.headers["content-type"]
        seen["body"] = request.content
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

    result = await _client(handler).edit(
        model="gpt-image-2",
        prompt="make it blue",
        image=PNG,
        content_type="image/png",
        size="auto",
    )

    assert result.data == PNG
    assert seen["url"] == "https://gateway.example/v1/images/edits"
    assert str(seen["type"]).startswith("multipart/form-data")
    body = seen["body"]
    assert isinstance(body, bytes)
    assert b'name="model"' in body and b"gpt-image-2" in body
    assert b'name="prompt"' in body and b"make it blue" in body
    assert b'name="image"; filename="image.png"' in body and PNG in body


async def test_url_only_responses_are_refused_without_fetching() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"data": [{"url": "https://cdn.example/image.png"}]})

    with pytest.raises(ImageApiError) as error:
        await _client(handler).generate(model="m", prompt="p", size="auto")
    assert error.value.code == "image_unavailable"
    assert calls == ["https://gateway.example/v1/images/generations"]


@pytest.mark.parametrize(
    ("status", "code"),
    [(400, "image_rejected"), (403, "image_rejected"), (429, "image_unavailable"), (502, "image_unavailable")],
)
async def test_provider_errors_map_to_safe_codes(status: int, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": "secret upstream detail"}})

    with pytest.raises(ImageApiError) as error:
        await _client(handler).generate(model="m", prompt="p", size="auto")
    assert error.value.code == code
    assert "secret upstream detail" not in str(error.value)


async def test_timeouts_map_to_image_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(ImageApiError) as error:
        await _client(handler).generate(model="m", prompt="p", size="auto")
    assert error.value.code == "image_timeout"


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {"input_tokens": 12, "output_tokens": 4160, "total_tokens": 4172},
            ImageUsage(12, 4160, 4172),
        ),
        ({"prompt_tokens": 5, "completion_tokens": 7}, ImageUsage(5, 7, 12)),
        # A total below its parts is not trusted.
        ({"input_tokens": 5, "output_tokens": 7, "total_tokens": 1}, ImageUsage(5, 7, 12)),
        ({"input_tokens": -1, "output_tokens": 7}, None),
        ({"input_tokens": "5"}, None),
        ("garbage", None),
    ],
)
async def test_provider_usage_is_read_defensively(usage: object, expected: ImageUsage | None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(PNG).decode()}], "usage": usage},
        )

    result = await _client(handler).generate(model="m", prompt="p", size="auto")
    assert result.usage == expected


async def test_errors_say_whether_the_provider_answered() -> None:
    def answered(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"url": "https://cdn/x.png"}], "usage": {"input_tokens": 1, "output_tokens": 2}}
        )

    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "policy"})

    with pytest.raises(ImageApiError) as unusable:
        await _client(answered).generate(model="m", prompt="p", size="auto")
    with pytest.raises(ImageApiError) as rejected:
        await _client(refused).generate(model="m", prompt="p", size="auto")
    assert (unusable.value.accepted, unusable.value.usage) == (True, ImageUsage(1, 2, 3))
    assert (rejected.value.accepted, rejected.value.usage) == (False, None)
