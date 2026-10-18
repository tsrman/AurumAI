import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.config import settings


def _load_enforcement_migration() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "alembic"
        / "versions"
        / "0004_require_knowledge_base_id.py"
    )
    spec = importlib.util.spec_from_file_location("rag_migration_0004", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_enforcement_migration_refuses_to_guess_for_unmapped_documents() -> None:
    class Result:
        def scalar_one(self) -> int:
            return 3

    class Connection:
        def execute(self, statement: object) -> Result:
            return Result()

    migration = _load_enforcement_migration()

    with pytest.raises(RuntimeError, match="3 document"):
        migration.require_explicit_knowledge_base_backfill(Connection())


# Needs a database already migrated to rag-api head (RAG_API_DATABASE_URL).
# Without the marker this hard-fails on a connection refusal instead of being
# skipped, which breaks the documented offline command
# `cd rag-api && uv run pytest tests -m 'not integration'`. CI runs the whole
# suite unfiltered after `alembic upgrade head`, so coverage there is unchanged.
@pytest.mark.integration
@pytest.mark.asyncio
async def test_final_document_schema_requires_and_indexes_knowledge_base_id() -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            column = (
                await connection.execute(
                    text(
                        "SELECT is_nullable, data_type FROM information_schema.columns "
                        "WHERE table_name = 'documents' AND column_name = 'knowledge_base_id'"
                    )
                )
            ).one()
            indexes = (
                await connection.execute(
                    text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE tablename = 'documents' AND indexdef LIKE '%knowledge_base_id%'"
                    )
                )
            ).scalars().all()
    finally:
        await engine.dispose()

    assert column == ("NO", "uuid")
    assert "ix_documents_knowledge_base_id" in indexes
