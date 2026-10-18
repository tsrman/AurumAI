from fastapi import HTTPException
import httpx
from typing import Any, Dict, List, cast

from src.core.config import settings

class RagClient:
    def __init__(self) -> None:
        self.base_url = settings.RAG_API_URL
        self.timeout = 30.0

    async def _request(
        self,
        method: str,
        path: str,
        org_id: str | None,
        knowledge_base_id: str,
        **kwargs: Any,
    ) -> Any:
        headers = kwargs.pop("headers", {})
        # httpx requires str header values — org_id is Optional[str] on
        # UserInfo (matches services/model_access.py's convention), even
        # though every route calling in here already required an
        # authenticated user via require_permission.
        headers["X-Org-Id"] = org_id or ""
        headers["X-Knowledge-Base-Id"] = knowledge_base_id

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
                if response.status_code in (404, 422):
                    raise HTTPException(status_code=response.status_code, detail=response.json().get("detail", "Error"))
                response.raise_for_status()
                if response.status_code != 204:
                    return response.json()
                return None
            except httpx.RequestError as exc:
                raise HTTPException(status_code=503, detail=f"RAG service unavailable: {exc}")
            except httpx.HTTPStatusError as exc:
                raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)

    async def create_document(
        self,
        org_id: str | None,
        knowledge_base_id: str,
        title: str,
        content: str | None = None,
        *,
        source_key: str | None = None,
        source_filename: str | None = None,
        operation_id: str | None = None,
    ) -> Dict[str, Any]:
        """Create a document from content or an object-storage source_key.

        rag-api requires exactly one of these two inputs.
        """
        payload: Dict[str, Any] = {
            "knowledge_base_id": knowledge_base_id,
            "title": title,
        }
        if operation_id is not None:
            payload["operation_id"] = operation_id
        if content is not None:
            payload["content"] = content
        if source_key is not None:
            payload["source_key"] = source_key
            payload["source_filename"] = source_filename
        return cast(
            Dict[str, Any],
            await self._request(
                "POST", "/v1/documents", org_id, knowledge_base_id, json=payload
            ),
        )

    async def list_documents(
        self, org_id: str | None, knowledge_base_id: str
    ) -> List[Dict[str, Any]]:
        return cast(
            List[Dict[str, Any]],
            await self._request("GET", "/v1/documents", org_id, knowledge_base_id),
        )

    async def get_document(
        self, org_id: str | None, knowledge_base_id: str, document_id: str
    ) -> Dict[str, Any]:
        return cast(
            Dict[str, Any],
            await self._request(
                "GET", f"/v1/documents/{document_id}", org_id, knowledge_base_id
            ),
        )

    async def delete_document(
        self, org_id: str | None, knowledge_base_id: str, document_id: str
    ) -> None:
        await self._request(
            "DELETE", f"/v1/documents/{document_id}", org_id, knowledge_base_id
        )

    async def search(
        self,
        org_id: str | None,
        knowledge_base_id: str,
        *,
        query: str,
        limit: int,
    ) -> Dict[str, Any]:
        return cast(
            Dict[str, Any],
            await self._request(
                "POST",
                "/v1/search",
                org_id,
                knowledge_base_id,
                json={"query": query, "limit": limit},
            ),
        )

rag_client = RagClient()
