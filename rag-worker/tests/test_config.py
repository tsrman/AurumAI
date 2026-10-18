import pytest
from pydantic import ValidationError

from src.config import Settings


def test_database_url_has_no_insecure_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """database_url must be a required field — no
    hardcoded rag_app:rag_app fallback for a deployment that forgets to set
    RAG_WORKER_DATABASE_URL (docker-compose.yml always sets it explicitly,
    so this only matters for anyone running the service outside compose)."""
    monkeypatch.delenv("RAG_WORKER_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="database_url"):
        Settings()
