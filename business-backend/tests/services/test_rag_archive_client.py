import importlib.util
import uuid
import pytest
import httpx


@pytest.mark.asyncio
async def test_private_archive_client_sends_only_exact_identity_and_rejects_wrong_receipt():
    assert importlib.util.find_spec("src.services.rag_archive_client") is not None
    from src.services.rag_archive_client import RagArchiveClient, RagArchiveError

    ids = [uuid.uuid4() for _ in range(3)]
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "archive_id": str(uuid.uuid4()),
                "generation": 2,
                "state": "verified",
                "receipt_hash": "a" * 64,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = RagArchiveClient(http, "http://rag-archive:8111", "test-fixture-secret")
        with pytest.raises(RagArchiveError, match="rag_archive_response_invalid"):
            await client.command(
                "export", org_id=ids[0], knowledge_base_id=ids[1], archive_id=ids[2], generation=2
            )
    import json

    assert json.loads(seen[0].content) == {
        "org_id": str(ids[0]),
        "knowledge_base_id": str(ids[1]),
        "archive_id": str(ids[2]),
        "generation": 2,
        "dry_run": False,
    }
    assert seen[0].headers["X-Archive-Service-Secret"] == "test-fixture-secret"


@pytest.mark.asyncio
async def test_inspect_discovers_independent_rag_generation_without_job_identity():
    from src.services.rag_archive_client import RagArchiveClient

    org, kb = uuid.uuid4(), uuid.uuid4()

    async def handler(request):
        import json

        assert json.loads(request.content) == {"org_id": str(org), "knowledge_base_id": str(kb)}
        return httpx.Response(200, json={"state": "active", "generation": 7, "archive_id": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        inspection = await RagArchiveClient(http, "http://rag-archive:8111", "fixture").inspect(
            org_id=org, knowledge_base_id=kb
        )
    assert inspection.generation == 7 and inspection.state == "active"
