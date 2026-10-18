import os

# database_url has no default anymore (see src/config.py)
# — tests need something set before any test module imports src.config,
# which conftest.py (loaded before collection) guarantees.
os.environ.setdefault(
    "RAG_API_DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test"
)
