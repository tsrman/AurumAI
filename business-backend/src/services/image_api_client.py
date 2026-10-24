"""Client for an OpenAI-compatible Images API (generations and edits).

Only inline ``b64_json`` results are accepted. A provider that answers with a
URL is treated as unavailable: fetching that URL from the backend would be an
outbound request to an arbitrary host outside the web egress boundary.
Provider error bodies are never surfaced; callers get a short code.
"""

from typing import Any

import httpx

from src.core.config import settings
from src.services.image_content import ImageContentError, decode_base64_image


class ImageApiError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ImageApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        verify: bool | str = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._verify = verify
        self._transport = transport

    async def generate(self, *, model: str, prompt: str, size: str) -> bytes:
        payload = {"model": model, "prompt": prompt, "n": 1, "size": size}
        return await self._request("generations", json=payload)

    async def edit(
        self,
        *,
        model: str,
        prompt: str,
        image: bytes,
        content_type: str,
        size: str,
    ) -> bytes:
        extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[content_type]
        return await self._request(
            "edits",
            data={"model": model, "prompt": prompt, "n": "1", "size": size},
            files={"image": (f"image.{extension}", image, content_type)},
        )

    async def _request(self, operation: str, **kwargs: Any) -> bytes:
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                verify=self._verify,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/v1/images/{operation}", headers=headers, **kwargs
                )
        except httpx.TimeoutException as exc:
            raise ImageApiError("image_timeout") from exc
        except httpx.HTTPError as exc:
            raise ImageApiError("image_unavailable") from exc
        if response.status_code in (400, 403, 422):
            # Invalid prompt, content policy or unsupported size: the model
            # can rephrase, retrying the same request cannot help.
            raise ImageApiError("image_rejected")
        if response.is_error:
            raise ImageApiError("image_unavailable")
        try:
            body = response.json()
            item = body["data"][0]
            encoded = item["b64_json"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ImageApiError("image_unavailable") from exc
        if not isinstance(encoded, str) or not encoded:
            raise ImageApiError("image_unavailable")
        try:
            return decode_base64_image(encoded, max_bytes=settings.IMAGES_MAX_BYTES)
        except ImageContentError as exc:
            raise ImageApiError("image_unavailable") from exc


def configured_image_api_client() -> ImageApiClient:
    return ImageApiClient(
        base_url=settings.IMAGES_API_URL or settings.LLM_GATEWAY_URL,
        api_key=settings.IMAGES_API_KEY.get_secret_value() or settings.GATEWAY_API_KEY,
        timeout_seconds=settings.IMAGES_TIMEOUT_SECONDS,
        verify=settings.gateway_verify,
    )
