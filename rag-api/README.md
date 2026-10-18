# AurumAI Document metadata and hybrid retrieval API

Owns its document schema/migrations and Milvus operations. Runtime rag_app is not schema owner or a migration role. Private archive/lifecycle routes use separate credentials/roles and are not mounted in the ordinary API. Source ownership, generation and index receipts are authoritative.

## Develop and verify

```bash
uv sync --frozen
uv run ruff check src tests
uv run mypy src
uv run pytest tests -v
```

See [the root quickstart](../QUICKSTART.md), [architecture](../docs/ARCHITECTURE.md),
[configuration](../docs/CONFIGURATION.md) and [testing](../docs/TESTING.md).
Project-owned code uses AGPL-3.0-only; dependencies retain upstream notices.
