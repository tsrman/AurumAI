# AurumAI Skills and MCP Temporal worker

Tenant MCP uses SSE; stdio is explicitly operator-global only. Backend camelCase responses are normalized to internal snake_case fields. Tenant code executes in sandbox sessions, never the shared worker environment.

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
