from typing import Any, Dict

import httpx

from src.config import settings

class RagClient:
    def __init__(self) -> None:
        self.base_url = settings.backend_api_url.rstrip("/")
        self.timeout = 60.0

    async def search(
        self,
        query: str,
        limit: int,
        *,
        knowledge_base_id: str,
        session_token: str,
    ) -> Dict[str, Any]:
        headers = {"Authorization": f"Bearer {session_token}"}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/v1/knowledge-bases/{knowledge_base_id}/search",
                    headers=headers,
                    json={"query": query, "limit": limit},
                )
                response.raise_for_status()
                data = response.json()
                
                return {
                    "results": [
                        {
                            "content": r.get("content"),
                            "relevance_score": r.get("relevance_score"),
                        }
                        for r in data.get("results", [])
                    ],
                    "warnings": data.get("warnings", []),
                }
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Backend RAG request failed: {exc}")

rag_client = RagClient()
