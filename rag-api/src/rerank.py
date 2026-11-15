"""Cross-encoder reranking through an Infinity-compatible ``/rerank`` endpoint.

Fusion of the dense and BM25 legs gives a good candidate list; a cross-encoder reads the
query and each passage together and orders them more accurately. Any failure raises
RerankError, and the caller falls back to the fusion order: reranking improves results,
it is never a reason for a search to fail.
"""

import httpx

from src.config import settings


class RerankError(Exception):
    pass


def rerank_enabled() -> bool:
    return bool(settings.rerank_url)


async def rerank_passages(query: str, passages: list[str]) -> list[float]:
    """One relevance score per passage, in the order the passages were given."""
    if not passages:
        return []
    documents = [passage[: settings.rerank_max_chars] for passage in passages]
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{settings.rerank_url.rstrip('/')}/rerank",
                json={
                    "model": settings.rerank_model,
                    "query": query,
                    "documents": documents,
                    "top_n": len(documents),
                    "return_documents": False,
                },
                timeout=settings.rerank_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RerankError(f"reranker unavailable: {type(exc).__name__}") from exc

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or len(results) != len(documents):
        raise RerankError("reranker did not score every passage")
    scores: list[float | None] = [None] * len(documents)
    for item in results:
        index = item.get("index") if isinstance(item, dict) else None
        score = item.get("relevance_score") if isinstance(item, dict) else None
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < len(documents)
            or scores[index] is not None
            or not isinstance(score, (int, float))
            or isinstance(score, bool)
        ):
            raise RerankError("reranker returned an invalid result")
        scores[index] = float(score)
    return [score for score in scores if score is not None]
