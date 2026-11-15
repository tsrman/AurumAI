from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src import rerank
from src.config import Settings


def _client(payload: dict | None = None, status: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload or {}
    if status >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "bad", request=MagicMock(), response=response
        )
    else:
        response.raise_for_status = MagicMock()
    client = MagicMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setattr(rerank.settings, "rerank_url", "http://rerank:7997")
    monkeypatch.setattr(rerank.settings, "rerank_model", "reranker-model")
    monkeypatch.setattr(rerank.settings, "rerank_timeout_seconds", 7.5)
    monkeypatch.setattr(rerank.settings, "rerank_max_chars", 10)


def test_rerank_is_off_until_a_url_is_configured(monkeypatch) -> None:
    monkeypatch.setattr(rerank.settings, "rerank_url", "")
    assert rerank.rerank_enabled() is False
    monkeypatch.setattr(rerank.settings, "rerank_url", "http://rerank:7997")
    assert rerank.rerank_enabled() is True


def test_settings_default_to_no_reranker(monkeypatch) -> None:
    monkeypatch.delenv("RAG_API_RERANK_URL", raising=False)
    assert Settings().rerank_url == ""


@pytest.mark.asyncio
async def test_rerank_posts_the_query_and_truncated_passages() -> None:
    client = _client({"results": [{"index": 0, "relevance_score": 0.2}, {"index": 1, "relevance_score": 0.9}]})
    with patch("httpx.AsyncClient", return_value=client):
        scores = await rerank.rerank_passages("question", ["a" * 50, "short"])

    url = client.post.call_args.args[0]
    body = client.post.call_args.kwargs["json"]
    assert url == "http://rerank:7997/rerank"
    assert body["model"] == "reranker-model"
    assert body["query"] == "question"
    assert body["documents"] == ["a" * 10, "short"]
    assert body["top_n"] == 2
    assert client.post.call_args.kwargs["timeout"] == 7.5
    assert scores == [0.2, 0.9]


@pytest.mark.asyncio
async def test_rerank_maps_scores_back_by_index_not_by_position() -> None:
    client = _client({"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]})
    with patch("httpx.AsyncClient", return_value=client):
        assert await rerank.rerank_passages("q", ["x", "y"]) == [0.1, 0.9]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"results": []},
        {"results": [{"index": 0, "relevance_score": 1.0}]},
        {"results": [{"index": 0, "relevance_score": 1.0}, {"index": 0, "relevance_score": 1.0}]},
        {"results": [{"index": 0, "relevance_score": 1.0}, {"index": 5, "relevance_score": 1.0}]},
        {"results": [{"index": 0, "relevance_score": "x"}, {"index": 1, "relevance_score": 1.0}]},
    ],
)
async def test_rerank_rejects_an_answer_that_does_not_score_every_passage(payload) -> None:
    with patch("httpx.AsyncClient", return_value=_client(payload)):
        with pytest.raises(rerank.RerankError):
            await rerank.rerank_passages("q", ["x", "y"])


@pytest.mark.asyncio
async def test_rerank_turns_transport_and_status_failures_into_rerank_errors() -> None:
    with patch("httpx.AsyncClient", return_value=_client(status=503)):
        with pytest.raises(rerank.RerankError):
            await rerank.rerank_passages("q", ["x"])

    broken = _client()
    broken.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    with patch("httpx.AsyncClient", return_value=broken):
        with pytest.raises(rerank.RerankError):
            await rerank.rerank_passages("q", ["x"])
