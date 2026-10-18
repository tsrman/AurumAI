# AurumAI Administration browser application

Separate Keycloak client/audience and API8001 for grants/models/analytics/skills/agent/workflow authoring. Preserve live authorization and version checks. Runtime RUNTIME_* and source/license pages do not require an image rebuild.

## Develop and verify

```bash
npm ci
npm run dev
npm run lint -- --max-warnings=0
npm run typecheck
npm run test
npm run build
```

See [the root quickstart](../QUICKSTART.md), [architecture](../docs/ARCHITECTURE.md),
[configuration](../docs/CONFIGURATION.md) and [testing](../docs/TESTING.md).
Project-owned code uses AGPL-3.0-only; dependencies retain upstream notices.
