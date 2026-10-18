from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

router = APIRouter()


@router.get("/metrics")
async def metrics() -> Response:
    """Prometheus text-exposition scrape endpoint.

    Unauthenticated, same trust level as /health and /ready — it only ever
    exposes counts/latencies by route template, never request/response
    bodies or identifiers.
    """
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
