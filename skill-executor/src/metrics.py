import time

from fastapi import Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

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


def _route_template(request: Request) -> str:
    """Route template (/sessions/{session_id}), not the concrete URL — keeps
    label cardinality bounded and doesn't leak session IDs through metric
    labels. A genuinely unmatched path (no route matched at all) is labeled
    "unmatched" instead of the raw path."""
    if request.scope.get("route") is None:
        return "unmatched"
    path = request.url.path
    for name, value in (request.scope.get("path_params") or {}).items():
        if isinstance(value, (str, int, float)):
            path = path.replace(str(value), "{" + name + "}", 1)
    return path[:255]


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start

        path = _route_template(request)
        REQUEST_COUNT.labels(method=request.method, path=path, status=str(response.status_code)).inc()
        REQUEST_DURATION_SECONDS.labels(method=request.method, path=path).observe(duration)

        return response


async def metrics_endpoint() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
