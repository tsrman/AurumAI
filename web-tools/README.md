# AurumAI Bounded web search/read adapter

Ingress uses an independent service credential. URL/DNS/address pinning, redirects, bounded parsing and network policy form the outbound boundary. Required IPv4/IPv6/operator denies match Compose/Helm. Public search availability does not prove SSRF enforcement.

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
