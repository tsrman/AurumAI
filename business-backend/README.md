# AurumAI Business and administration API

Two entry points: src.main_business:app on8000 and src.main_admin:app on8001. Use separate Keycloak audiences, session-aware internal routes, FORCE RLS and dedicated migration/runtime roles. Apply current migrations to a separate test DB; never use a live outbox-dispatcher database.

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
