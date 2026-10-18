"""Minimal authenticated client for the internal web-tools boundary."""

import asyncio
import json
from typing import Any, cast

import httpx

from src.core.config import settings, validated_web_tools_origin
from src.schemas.web_tools import (
    WebErrorCode,
    WebReadServiceResponse,
    WebSearchServiceResponse,
)


_ERROR_CODES = {
    "web_disabled",
    "web_access_denied",
    "web_limit",
    "web_timeout",
    "web_unavailable",
    "web_url_blocked",
    "web_too_large",
    "unsupported_document",
}
MAX_WEB_TOOLS_RESPONSE_BYTES = 512 * 1024


class WebToolsClientError(ValueError):
    def __init__(self, code: WebErrorCode, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable and code in {"web_timeout", "web_unavailable"}
        super().__init__(code)


class WebToolsClient:
    def __init__(
        self,
        *,
        base_url: str,
        service_secret: str,
        timeout_seconds: float = 35,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if len(service_secret) < 16:
            raise ValueError("web-tools service secret is missing")
        self._base_url = validated_web_tools_origin(base_url)
        self._service_secret = service_secret
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def execute(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool not in {"web_search", "web_read"}:
            raise WebToolsClientError("web_unavailable")
        path = "/search" if tool == "web_search" else "/read"
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=self._timeout_seconds,
                    trust_env=False,
                    follow_redirects=False,
                    transport=self._transport,
                ) as client:
                    async with client.stream(
                        "POST",
                        f"{self._base_url}{path}",
                        headers={"Authorization": f"Bearer {self._service_secret}"},
                        json=args,
                    ) as response:
                        content_type = response.headers.get("content-type", "").split(";", 1)[0]
                        if content_type != "application/json" and not content_type.endswith(
                            "+json"
                        ):
                            raise WebToolsClientError("web_unavailable")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > MAX_WEB_TOOLS_RESPONSE_BYTES:
                                raise WebToolsClientError("web_unavailable")
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise WebToolsClientError("web_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise WebToolsClientError("web_unavailable", retryable=True) from exc
        if response.status_code >= 400:
            code: object = None
            retryable = False
            try:
                error_payload = json.loads(body)
                detail = error_payload.get("detail")
                if isinstance(detail, dict):
                    code = detail.get("code")
                    retryable = detail.get("retryable") is True
            except (UnicodeDecodeError, ValueError, AttributeError):
                pass
            if isinstance(code, str) and code in _ERROR_CODES:
                raise WebToolsClientError(cast(WebErrorCode, code), retryable=retryable)
            raise WebToolsClientError("web_unavailable")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError) as exc:
            raise WebToolsClientError("web_unavailable") from exc
        if not isinstance(payload, dict):
            raise WebToolsClientError("web_unavailable")
        response_type = WebSearchServiceResponse if tool == "web_search" else WebReadServiceResponse
        try:
            normalized = response_type.model_validate(payload).model_dump(mode="json")
        except ValueError as exc:
            raise WebToolsClientError("web_unavailable") from exc
        return normalized


def configured_web_tools_client() -> WebToolsClient:
    return WebToolsClient(
        base_url=settings.WEB_TOOLS_URL,
        service_secret=settings.WEB_TOOLS_SERVICE_SECRET.get_secret_value(),
    )
