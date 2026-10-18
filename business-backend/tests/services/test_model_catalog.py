"""The catalog every authorization decision about models reads.

`routers/models.py` keeps its own reader that falls back to a mock list so the
picker still shows something in DEBUG. This one must not: a guessed id would
become a real grant, or would let `resource_registry` validate a grant against a
model that does not exist.
"""

import httpx
import pytest

from src.services import model_catalog


@pytest.fixture(autouse=True)
def real_gateway(monkeypatch):
    # DEBUG without a gateway key short-circuits to a static list; these tests
    # are about the HTTP path.
    monkeypatch.setattr(model_catalog.settings, "DEBUG", False)
    monkeypatch.setattr(model_catalog.settings, "GATEWAY_API_KEY", "test-key")


def _transport(handler):
    class _Client(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.pop("verify", None)
            super().__init__(*args, transport=httpx.MockTransport(handler), **kwargs)

    return _Client


async def test_an_unreachable_gateway_yields_no_models_rather_than_a_guess(
    monkeypatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gateway down", request=request)

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", _transport(handler))

    snapshot = await model_catalog.gateway_catalog_snapshot()

    assert snapshot == model_catalog.GatewayCatalogSnapshot(
        model_catalog.GatewayCatalogStatus.UNAVAILABLE,
        (),
    )
    assert await model_catalog.gateway_models() == []


async def test_a_gateway_error_status_yields_no_models(monkeypatch) -> None:
    monkeypatch.setattr(
        model_catalog.httpx,
        "AsyncClient",
        _transport(lambda request: httpx.Response(503)),
    )

    assert await model_catalog.gateway_models() == []


async def test_gateway_snapshot_identifies_debug_mock_catalog(monkeypatch) -> None:
    monkeypatch.setattr(model_catalog.settings, "DEBUG", True)
    monkeypatch.setattr(model_catalog.settings, "GATEWAY_API_KEY", "")

    snapshot = await model_catalog.gateway_catalog_snapshot()

    assert snapshot.status is model_catalog.GatewayCatalogStatus.MOCK
    assert [row["id"] for row in snapshot.models] == [
        "gpt-4o-mini",
        "gpt-4o",
        "claude-3-5-sonnet",
    ]


async def test_gateway_snapshot_identifies_a_valid_live_catalog(monkeypatch) -> None:
    monkeypatch.setattr(
        model_catalog.httpx,
        "AsyncClient",
        _transport(
            lambda request: httpx.Response(
                200,
                json={"data": [{"id": "provider/chat"}, "invalid-row"]},
            )
        ),
    )

    snapshot = await model_catalog.gateway_catalog_snapshot()

    assert snapshot.status is model_catalog.GatewayCatalogStatus.AVAILABLE
    assert snapshot.models == ({"id": "provider/chat"},)


@pytest.mark.parametrize("payload", [{}, {"data": None}])
async def test_gateway_snapshot_rejects_malformed_object_payloads(
    monkeypatch,
    payload,
) -> None:
    monkeypatch.setattr(
        model_catalog.httpx,
        "AsyncClient",
        _transport(lambda request: httpx.Response(200, json=payload)),
    )

    snapshot = await model_catalog.gateway_catalog_snapshot()

    assert snapshot == model_catalog.GatewayCatalogSnapshot(
        model_catalog.GatewayCatalogStatus.UNAVAILABLE,
        (),
    )


async def test_gateway_snapshot_accepts_an_explicit_empty_catalog(monkeypatch) -> None:
    monkeypatch.setattr(
        model_catalog.httpx,
        "AsyncClient",
        _transport(lambda request: httpx.Response(200, json={"data": []})),
    )

    snapshot = await model_catalog.gateway_catalog_snapshot()

    assert snapshot == model_catalog.GatewayCatalogSnapshot(
        model_catalog.GatewayCatalogStatus.AVAILABLE,
        (),
    )


async def test_chat_model_ids_drops_non_chat_models_and_duplicates(monkeypatch) -> None:
    monkeypatch.setattr(
        model_catalog.httpx,
        "AsyncClient",
        _transport(
            lambda request: httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "openai/gpt-4o-mini"},
                        {"id": "openai/text-embedding-3-small"},
                        {"id": "openai/gpt-4o-mini"},
                        {"id": ""},
                        {"no_id": True},
                        "not-an-object",
                    ]
                },
            )
        ),
    )

    assert await model_catalog.chat_model_ids() == ("openai/gpt-4o-mini",)


async def test_provisioning_reads_the_catalog_with_a_short_timeout(monkeypatch) -> None:
    """Provisioning happens inside the first login of a new organization, so the
    default 30-second read would make a slow gateway a slow login."""
    seen: list[float] = []

    async def fake_gateway_models(*, timeout: float = 30.0) -> list[dict[str, object]]:
        seen.append(timeout)
        return []

    monkeypatch.setattr(model_catalog, "gateway_models", fake_gateway_models)

    await model_catalog.provisioning_models()

    assert seen == [model_catalog.PROVISIONING_CATALOG_TIMEOUT_SECONDS]
    assert model_catalog.PROVISIONING_CATALOG_TIMEOUT_SECONDS < 30.0
