import time

from fastapi import Request, Response
from prometheus_client import Counter, Histogram
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from src.middleware.audit import _route_template

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
)


def _metrics_path(request: Request) -> str:
    # request.scope["route"] is only set once Starlette's router has actually
    # matched a route — a genuinely unknown path (never routed) is labeled
    # "unmatched" instead of the raw URL, so garbage/probe paths can't create
    # unbounded label cardinality the way a real route template can't either.
    if request.scope.get("route") is None:
        return "unmatched"
    return _route_template(request)


class MetricsMiddleware(BaseHTTPMiddleware):
    """Records request count and latency by method/route-template/status.

    Runs on every request (unlike AuditMiddleware, which only records
    mutating /v1 calls) — metrics are aggregate counts, not a record of who
    did what, so there is no reason to narrow them the same way.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        path = _metrics_path(request)
        REQUEST_COUNT.labels(method=request.method, path=path, status=str(response.status_code)).inc()
        REQUEST_DURATION_SECONDS.labels(method=request.method, path=path).observe(duration)

        return response
