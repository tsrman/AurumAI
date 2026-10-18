# AurumAI Durable document parsing and indexing worker

Downloads source files, parses/chunks/embeds them and records durable indexing receipts. Prepare target model caches/licenses for offline use. Legacy inputs retain their replay path; do not invent archive provenance for an old queue entry.

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
