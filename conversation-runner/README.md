# AurumAI Isolated conversation Temporal worker

Executes ConversationWorkflow, MultiAgentWorkflow, AgentWorkflow, evaluation and code-skill orchestration. This is the real workflow implementation; the backend ConversationWorkflow is a signal stub. Register every new activity in src/worker.py and protect changed commands with permanent replay markers. Read mutable pool/session settings in activities, not workflow imports.

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
