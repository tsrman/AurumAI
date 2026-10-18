# Agent and maintainer guidance

AurumAI is a multi-agent application platform. Human documentation starts at
[README](README.md), [architecture](docs/ARCHITECTURE.md) and
[testing](docs/TESTING.md). Work on a feature branch/worktree; preserve changes
made by other contributors.

## Workflow

- Follow TDD for behavior changes: reproduce the failure before implementing a fix.
- Commit, merge, push or publish only when explicitly requested. Preparing a public
  candidate does not authorize changing repository visibility or publishing it.
- Use uv for Python services, not Poetry. Frontends use npm/frozen lockfiles.
- Run affected service lint/type/tests and required deployment/harness checks before
  claiming completion. Report failed, skipped and blocked coverage honestly.
- Write all documentation and all code comments in English: Markdown/docs, READMEs,
  docstrings, inline/block comments in every language and configuration file (Python,
  TypeScript, shell, YAML, Dockerfiles, SQL). This applies to new and edited text alike.
- Source files contain no Russian text at all, including runtime strings, LLM prompts,
  log messages, fixtures and tests. The only Russian text in the repository is the
  `messages/ru.json` UI catalogs. Tests that need Cyrillic data build it from `\u`
  escapes. The release language check enforces this.
- UI text lives only in `business-ui/messages/` and `admin-ui/messages/` (`en.json`,
  `ru.json`, identical keys and ICU arguments). Components use next-intl; no
  user-visible literal goes into `src/`. Unit tests render the English catalog and
  Playwright pins `en-US`; Russian rendering is asserted with values imported from
  `ru.json`. `tests/i18n` guards catalog parity and stray literals.
- Preserve private journals/history/snapshots on unresolved cleanup. Do not publish
  credentials, raw HTTP, browser tokens/traces/storage state or operator archives.
- Read the approved spec/plan and maintain task evidence; do not restart completed
  work after a context reset. Internal planning/development history is excluded
  from public source candidates by the release manifest.

## Required checks

In each Python service: `uv run ruff check src tests`, `uv run mypy src`, and the
appropriate `uv run pytest tests` suite. Backend/RAG DB tests need their own migrated
PostgreSQL test database; never reuse a live API/outbox-dispatcher database.

In both UIs: `npm run lint -- --max-warnings=0`, `npm run typecheck`, `npm run test`,
`npm run build`. Repository harness: install `scripts` npm dependencies, then run
`uv run --project business-backend pytest scripts/tests`. Documentation/release
checks validate links, Mermaid, English prose, manifest inventory and redacted secrets.

Organization-mutating/browser smoke wrappers run exclusively and sequentially.
Real provider/Kubernetes CNI/one-hour deadlines are separate manual acceptance;
a unit/short/Helm check does not prove them. Dedicated browser privacy settings
must remain enabled. Consult [testing](docs/TESTING.md) for exact commands/coverage.

## Architectural constraints

### Runtime and Temporal

- Backend ConversationWorkflow is a typed signal stub; actual workflow code is
  in conversation-runner. Keep signal signatures synchronized.
- All workflows use the common `<tool_call>{...}</tool_call>` parser; do not reintroduce
  an alternative action format.
- Model IDs are configurable. Calls remain streaming through the API gateway proxy;
  do not add incompatible response_format/nonstreaming requests.
- A conversation runs in one owned container/Pod. Multi-agent child nodes run in
  that worker, not separate agent containers. Preserve graph/parallelism/output bounds.
- Preserve permanent patch markers and replay old histories when changing commands,
  activity registration or child inputs. Register every new activity in src/worker.py.
- Pool-bound settings changed after workflow import are read through activities outside
  the Temporal sandbox. Workflow imports do not observe later session-token mutations.
- New evaluation requires an explicit operator model; keep evaluator-explicit-model-v1.
  Loop N+1 context receives only results N; keep loop-iteration-context-v1.
- Questions rollout is server-owned/default-off and passed in turn signals. Disabling
  new reservations does not block existing durable lifecycle operations; keep user-questions-v1.

### Identity, authorization and data

- Business/admin browser JWT audiences differ. Internal runner/tool-worker routes use
  get_current_user_or_session plus get_service_db_session, not browser-only dependencies.
- Session tokens carry identity, not permission snapshots. Recheck live grants before
  preparation, each workflow step, synthesis and pre-persist external results.
- Keycloak owns membership. Platform capabilities/resource grants own authorization.
  Organization defaults are configuration, not ACLs. Same-org admin bypass does not
  bypass FORCE RLS or runtime-owner controls.
- Set app.current_org_id per scoped DB transaction. Migration/runtime/lifecycle roles
  are separate; ordinary API/RAG accounts never inherit schema-owner/archive privileges.
- Preserve UUID registries, unique/idempotency keys, FK relationships and typed archive
  values when changing partition/lifecycle code. Audit/billing/usage are not expendable fixtures.
- Source ownership, generation and exact terminal receipts fence external indexing/
  upload/purge/restore. An unknown legacy owner/history/remote reply blocks destructive work.
- Archive restoration uses the established archive and a new generation; do not rerun
  historical workflows, re-embed vectors or overwrite foreign unique resources.

### Tools, network and presentation

- Tenant MCP uses SSE. Stdio is explicitly operator-global with org_id=NULL; missing
  ownership is not global. Endpoint/transport changes cannot forward old credentials
  without explicit replacement input. Tenant code runs in sandbox, not tool-worker.
- Normalize backend camelCase skill JSON for tool-worker's snake_case consumers.
- Docker runner stays on its configured network. Kubernetes namespaces/RBAC/NetworkPolicy
  are distinct for applications, runners and sandboxes; standard pods/exec is not label-scoped.
- Pod deletion requires release/conversation label proof and UID precondition. Sandbox
  PID enforcement is a RuntimeClass/CRI/node concern, not a portable Pod spec field.
- Web is canonical web.access/default-off. Required deny ranges must match adapter,
  guard and Helm; operator CIDRs apply to all layers. Real IPv4/IPv6 positive controls
  are required before claiming SSRF/CNI deny coverage.
- Public workflow events contain only safe allowlisted status metadata. Keep briefs,
  internal outputs/thoughts/tool payloads out of user SSE; only successful synthesis
  persists the final assistant response.
- Frontend URLs/source links use per-request RUNTIME_*, not NEXT_PUBLIC_ build values.
  Keep complete-history loading, call-ID tool-card merging, durable SSE reconnect and
  permanent-authorization-denial stop conditions.

## Exact-owned recovery

Before removing fixture metadata/runtime, verify the exact Temporal tree: recorded
root input/queue/run UUID, reciprocal parent/run/initiation, allowed child IDs and
terminal closure. Terminate only verified RUNNING exact handles; preserve closed history.
Never use prefix SQL, broad workflow search/terminate or replacement namespaces to
substitute for missing established proof.

Questions/web Kubernetes wrappers preserve runtime, namespace/release/cluster,
Compose dependencies, work directory/kubeconfig and private journal on failed cleanup,
verify-clean or missing established state. Browser flows use actual Helm Ingress through
loopback NodePort; REST recovery tunnels do not become SSE browser infrastructure.

Do not retry an uncertain external/membership mutation or issue a racing compensating
write merely because the first response was lost. Preserve original operation identity
and reconcile its authoritative outcome. Archive recovery requirements are in
[the operator runbook](docs/operations/data-archive.md).

Authorization UI acceptance: `./smoke-test-authorization-ui.sh` verifies both Keycloak audiences and live resource grants. Run it exclusively with trace capture disabled; keep the private journal if exact-owned recovery fails. See the testing guide for prerequisites.

Knowledge UI acceptance: `./smoke-test-knowledge-ui.sh` verifies Admin UI → Business UI → MinIO/Temporal/Milvus lifecycle. It must not overlap organization mutations. It requires the current stack APP_SECRET_KEY, uses a private recovery journal and preserves it on cleanup failure.

For deterministic MCP/code acceptance, export the existing stack secret before applying the overlay:

```bash
export APP_SECRET_KEY=<current-stack-secret>
docker compose -f docker-compose.yml -f docker-compose.e2e.yml --profile mcp --profile skills-exec build backend backend-admin tool-worker mcp-everything mcp-fetch skill-executor test-gateway
docker compose -f docker-compose.yml -f docker-compose.e2e.yml --profile mcp --profile skills-exec up -d --no-build backend backend-admin tool-worker mcp-everything mcp-fetch skill-executor
# Run one exclusive acceptance scenario, then restore the ordinary gateway:
docker compose -f docker-compose.yml -f docker-compose.e2e.yml rm -sf test-gateway
docker compose -f docker-compose.yml up -d --force-recreate backend
```

Keep the exact-owned recovery journal if cleanup fails; restore infrastructure only as the scenario's recovery contract permits.
