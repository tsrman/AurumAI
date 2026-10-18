import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from src.middleware.metrics import MetricsMiddleware
from src.routers import metrics as metrics_router


@pytest.fixture
def client():
    test_app = FastAPI()
    test_app.add_middleware(MetricsMiddleware)
    test_app.include_router(metrics_router.router)

    @test_app.get("/v1/widgets/{widget_id}")
    async def get_widget(widget_id: str) -> dict[str, str]:
        return {"id": widget_id}

    return httpx.AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test")


async def test_metrics_endpoint_serves_prometheus_text_format(client):
    async with client as c:
        response = await c.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


async def test_metrics_count_uses_route_template_not_raw_id(client):
    async with client as c:
        await c.get("/v1/widgets/11111111-1111-1111-1111-111111111111")
        await c.get("/v1/widgets/22222222-2222-2222-2222-222222222222")
        response = await c.get("/metrics")

    body = response.text
    assert 'path="/v1/widgets/{widget_id}"' in body
    assert "11111111-1111-1111-1111-111111111111" not in body
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body


async def test_metrics_labels_truly_unmatched_paths_as_unmatched(client):
    async with client as c:
        await c.get("/nope/does-not-exist")
        response = await c.get("/metrics")

    body = response.text
    assert 'path="unmatched"' in body
    assert "does-not-exist" not in body
