import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from src.metrics import MetricsMiddleware, metrics_endpoint


@pytest.fixture
def client():
    test_app = FastAPI()
    test_app.add_middleware(MetricsMiddleware)
    test_app.add_api_route("/metrics", metrics_endpoint, methods=["GET"])

    @test_app.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, str]:
        return {"id": session_id}

    return httpx.AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test")


@pytest.mark.asyncio
async def test_metrics_endpoint_serves_prometheus_text_format(client):
    async with client as c:
        response = await c.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


@pytest.mark.asyncio
async def test_metrics_count_uses_route_template_not_raw_id(client):
    async with client as c:
        await c.get("/sessions/11111111-1111-1111-1111-111111111111")
        response = await c.get("/metrics")

    body = response.text
    assert 'path="/sessions/{session_id}"' in body
    assert "11111111-1111-1111-1111-111111111111" not in body
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body


@pytest.mark.asyncio
async def test_metrics_labels_truly_unmatched_paths_as_unmatched(client):
    async with client as c:
        await c.get("/nope/does-not-exist")
        response = await c.get("/metrics")

    assert 'path="unmatched"' in response.text
    assert "does-not-exist" not in response.text
