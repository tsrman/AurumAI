import uuid
import pytest
import httpx
from unittest.mock import patch
from src.services.rag_client import RagClient


@pytest.mark.asyncio
async def test_document_create_carries_the_durable_business_operation_uuid():
    operation = uuid.uuid4()
    seen = []

    async def respond(request):
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(202, json={"id": str(uuid.uuid4())})

    transport = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with patch("src.services.rag_client.httpx.AsyncClient", return_value=transport):
        await RagClient().create_document(
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            "fixture",
            content="text",
            operation_id=str(operation),
        )
    assert seen[0]["operation_id"] == str(operation)
