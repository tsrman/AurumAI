import json
from pathlib import Path

from src.main import create_app
from src.schemas import WEB_TOOLS_CONTRACT_VERSION, ReadRequest, SearchRequest


class _Search:
    async def search(self, query, language, time_range):  # noqa: ANN001, ANN201
        from datetime import UTC, datetime

        from src.search import SearchHit

        del query, language, time_range
        return [
            SearchHit(
                title="Release",
                url="https://example.org/release",
                snippet="Release notes",
                published_at=datetime(2026, 9, 1, 12, 30, tzinfo=UTC),
            )
        ]


class _Reader:
    async def read(self, url):  # noqa: ANN001, ANN201
        from src.reader import ReadResult

        del url
        return ReadResult(
            requested_url="https://example.org/report.pdf",
            final_url="https://cdn.example.org/report.pdf",
            content_type="application/pdf",
            text="[Page 1] Report",
            title="Report",
            truncated=False,
            page_count=1,
        )


def test_service_owns_an_independent_copy_of_the_versioned_wire_contract() -> None:
    fixture = json.loads(
        (Path(__file__).parents[2] / "contracts/web-tools-v1.json").read_text()
    )

    assert WEB_TOOLS_CONTRACT_VERSION == fixture["version"] == 1
    assert SearchRequest.model_validate(fixture["search"]).model_dump(exclude_none=True) == fixture[
        "search"
    ]
    assert ReadRequest.model_validate(fixture["read"]).model_dump(exclude_none=True) == fixture[
        "read"
    ]


async def test_response_wire_contract_matches_backend_fixture() -> None:
    import httpx
    from httpx import ASGITransport

    fixture = json.loads(
        (Path(__file__).parents[2] / "contracts/web-tools-v1.json").read_text()
    )
    app = create_app(
        service_secret="unit-only-secret",
        search_provider=_Search(),
        read_provider=_Reader(),
        sandbox_checker=lambda: True,
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        search = await client.post(
            "/search",
            headers={"Authorization": "Bearer unit-only-secret"},
            json=fixture["search"],
        )
        read = await client.post(
            "/read",
            headers={"Authorization": "Bearer unit-only-secret"},
            json=fixture["read"],
        )

    assert search.json() == fixture["search_response"]
    assert read.json() == fixture["read_response"]
