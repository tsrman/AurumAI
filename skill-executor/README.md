# AurumAI Ephemeral code sandbox manager

Docker for trusted development or Kubernetes API/pods-exec for the production runtime. Sandbox namespaces are separate from runner/application namespaces; verify owned labels/UID on cleanup. Bounded rounds/time/network policies apply independently of model behavior.

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
