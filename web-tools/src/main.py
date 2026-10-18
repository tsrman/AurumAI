"""Authenticated internal boundary; outbound handlers stay fail-closed for now."""

from collections.abc import Awaitable, Callable
import hmac
from typing import Annotated, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, status

from src.config import Settings
from src.parser_sandbox import ParserFailed, sandbox_available
from src.policy import URLBlocked
from src.reader import PublicReader, ReadResult
from src.schemas import ReadRequest, SearchRequest
from src.search import SearchFailed, SearchHit, SearxngAdapter, TimeRange
from src.transport import FetchFailed, PinnedTransport


def _auth_dependency(service_secret: str) -> Callable[..., Awaitable[None]]:
    expected = service_secret.encode("utf-8")

    async def require_service_auth(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        scheme, separator, candidate = (authorization or "").partition(" ")
        supplied = candidate.encode("utf-8") if separator and scheme.lower() == "bearer" else b""
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid service credential",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_service_auth


class SearchProvider(Protocol):
    async def search(
        self,
        query: str,
        language: str | None,
        time_range: TimeRange | None,
    ) -> list[SearchHit]: ...


class ReadProvider(Protocol):
    async def read(self, url: str) -> ReadResult: ...


def _web_failure(code: str, *, retryable: bool = False) -> HTTPException:
    status_code = {
        "web_timeout": status.HTTP_504_GATEWAY_TIMEOUT,
        "web_too_large": status.HTTP_413_CONTENT_TOO_LARGE,
        "web_url_blocked": status.HTTP_422_UNPROCESSABLE_CONTENT,
        "unsupported_document": status.HTTP_422_UNPROCESSABLE_CONTENT,
    }.get(code, status.HTTP_503_SERVICE_UNAVAILABLE)
    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "retryable": retryable and code in {"web_timeout", "web_unavailable"},
        },
    )


def create_app(
    service_secret: str,
    *,
    search_provider: SearchProvider | None = None,
    read_provider: ReadProvider | None = None,
    sandbox_checker: Callable[[], bool] = sandbox_available,
    searxng_url: str = "http://searxng:8080",
    forbidden_cidrs: tuple[str, ...] = (),
) -> FastAPI:
    if len(service_secret) < 16 or not service_secret.strip():
        raise ValueError("service secret must contain at least 16 nonblank characters")

    app = FastAPI(title="AurumAI Web Tools", docs_url=None, redoc_url=None)
    require_service_auth = _auth_dependency(service_secret)
    # A service request performs exactly one public attempt. The durable
    # backend owns the logical two-attempt budget across retries and crash
    # lease takeovers, so nested retries cannot multiply it to four.
    search_adapter = search_provider or SearxngAdapter(
        endpoint=searxng_url, max_attempts=1, forbidden_cidrs=forbidden_cidrs
    )
    reader = read_provider or PublicReader(
        transport=PinnedTransport(max_attempts=1, forbidden_cidrs=forbidden_cidrs)
    )
    sandbox_ready = sandbox_checker()

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        # Readiness verifies only the local boundary. Public search health is
        # intentionally not part of platform availability.
        if not sandbox_ready:
            raise _web_failure("web_unavailable")
        return {"status": "ready"}

    @app.post("/search", dependencies=[Depends(require_service_auth)])
    async def search(data: SearchRequest) -> dict[str, list[SearchHit]]:
        try:
            results = await search_adapter.search(data.query, data.language, data.time_range)
        except SearchFailed as exc:
            raise _web_failure(exc.code, retryable=exc.retryable) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "invalid_search_query"},
            ) from exc
        return {"results": results}

    @app.post("/read", dependencies=[Depends(require_service_auth)])
    async def read(data: ReadRequest) -> ReadResult:
        if not sandbox_ready:
            raise _web_failure("web_unavailable")
        try:
            return await reader.read(data.url)
        except URLBlocked as exc:
            raise _web_failure("web_url_blocked") from exc
        except FetchFailed as exc:
            raise _web_failure(exc.code, retryable=exc.retryable) from exc
        except ParserFailed as exc:
            raise _web_failure(exc.code) from exc

    return app


def main() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run(
        create_app(
            settings.SERVICE_SECRET.get_secret_value(),
            searxng_url=str(settings.SEARXNG_URL),
            forbidden_cidrs=settings.forbidden_cidrs,
        ),
        host=settings.HOST,
        port=settings.PORT,
        access_log=False,
    )


if __name__ == "__main__":
    main()
