from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.rag_client import RagClient


def _make_httpx_client(json_body: dict) -> MagicMock:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = json_body

    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_search_uses_backend_authorization_boundary_and_maps_response():
    """The backend authorizes the live session before proxying to rag-api.

    rag-api's /v1/search returns
    results[].relevance_score (not "score") — the skill contract
    (docs/ARCHITECTURE.md) requires relevance_score to
    actually carry a value, not always come through as None."""
    client = _make_httpx_client(
        {
            "results": [{"content": "chunk text", "relevance_score": 0.87}],
            "warnings": [],
        }
    )

    with patch("httpx.AsyncClient", return_value=client):
        result = await RagClient().search(
            "query",
            5,
            knowledge_base_id="kb-1",
            session_token="session-token",
        )

    assert result == {
        "results": [{"content": "chunk text", "relevance_score": 0.87}],
        "warnings": [],
    }
    client.post.assert_awaited_once_with(
        "http://localhost:8000/v1/knowledge-bases/kb-1/search",
        headers={"Authorization": "Bearer session-token"},
        json={"query": "query", "limit": 5},
    )


@pytest.mark.asyncio
async def test_search_defaults_missing_relevance_score_to_none():
    client = _make_httpx_client({"results": [{"content": "chunk text"}], "warnings": []})

    with patch("httpx.AsyncClient", return_value=client):
        result = await RagClient().search(
            "query", 5, knowledge_base_id="kb-1", session_token="token"
        )

    assert result["results"] == [{"content": "chunk text", "relevance_score": None}]


@pytest.mark.asyncio
async def test_search_wraps_http_errors():
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
    client.__aenter__.return_value = client
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=client), pytest.raises(
        RuntimeError, match="Backend RAG request failed"
    ):
        await RagClient().search(
            "query", 5, knowledge_base_id="kb-1", session_token="token"
        )
