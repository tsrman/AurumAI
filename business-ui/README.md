# AurumAI Chat browser application

Next.js/TypeScript frontend for chat/history/knowledge and durable questions/workflow progress. Business-ui Keycloak audience is distinct from administration. Runtime server configuration uses RUNTIME_*; complete paginated history replaces UI state only after all pages load.

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
