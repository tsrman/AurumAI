# Testing AurumAI

Use the commands below inside the relevant service. Python services use uv and
strict Ruff/mypy. Frontends use npm, ESLint, TypeScript, Vitest and production builds.
CI covers these checks; live acceptance is separate and often exclusive.

## Install dependencies

```bash
cd business-backend
uv sync --frozen
```

Repeat for each Python service. For `business-ui`, `admin-ui` and `scripts`, run
`npm ci`. Scripts include the TypeScript parser used by the English-comment check;
harness tests must not depend on a developer's unrelated UI installation.

## Python services

```bash
uv run ruff check src tests
uv run mypy src
uv run pytest tests -v
```

Apply service migrations and use a separately created test database for backend
and RAG DB tests. Never point tests at a live API database: its outbox dispatcher
can consume test rows. Set `APP_DATABASE_URL`, not a similarly named ignored env.

```bash
APP_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/aurum_ai_test uv run alembic upgrade head
APP_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/aurum_ai_test uv run pytest tests -m 'not integration' -v
```

The test database needs pgvector/pg_partman and current roles/schema. Use the
project PostgreSQL image or equivalent prerequisites. Integration markers require
additional live dependencies; report skipped tests explicitly.

## Frontends

Run in both UI directories:

```bash
npm run lint -- --max-warnings=0
npm run typecheck
npm run test
npm run build
```

`next lint` is not the lint entry point. Business/admin Keycloak audiences are
separate. Runtime configuration/source URL behavior, complete chat history and
permanent-denial/reconnect behavior are covered by relevant tests.

## Harness and documentation

From the repository root, after installing backend and scripts dependencies:

```bash
uv run --project business-backend pytest scripts/tests -q
node scripts/check-mermaid.mjs
python3 scripts/check-release-language.py --root .
```

The default harness run does not require exporting a deployment's `APP_SECRET_KEY`.
The metadata-only attachment check supplies its own synthetic signing key and
fixture database URL to its subprocess without connecting to that database.
Live acceptance has separate credential and infrastructure prerequisites below.

The source-export manifest deliberately excludes private development history.
Language checks reject Russian text in every published file except the
`messages/ru.json` UI catalogs. The independent scanner reviews exported files and bounded archive content,
with redaction. See [release preparation](RELEASE.md).

## Live Compose acceptance

A built stack is required. Run shared-organization/browser wrappers sequentially,
never concurrently with restarts or other organization mutations. Most wrappers
use a deterministic gateway so assertions do not depend on provider behavior.

Core commands include `smoke-test.sh`, MCP/code skill, groups/quota/organization,
authorization, knowledge and multi-agent/workflow wrappers. Workflow tests cover
UI authoring/templates/routing/branching, explicit HITL negatives, restart and loops.
A wrapper succeeds only after its scenario, exact cleanup, grant restoration,
verify-clean and infrastructure restoration all succeed.

Questions have separate short/restart/hour variants:

```bash
QUESTION_ACCEPTANCE_KIND=short ./smoke-test-user-questions.sh
QUESTION_ACCEPTANCE_KIND=restart ./smoke-test-user-questions.sh
QUESTION_ACCEPTANCE_KIND=hour ./smoke-test-user-questions.sh
```

The hour variant uses a real 3600-second deadline; a shortened/test-clock scenario
does not substitute for it. HITL's dedicated timeout test image similarly does not
prove the production 1800-second deadline.

Web requires exclusive mode and its configured secrets/enforcement:

```bash
WEB_ACCEPTANCE_EXCLUSIVE=1 WEB_ACCEPTANCE_KIND=controlled ./smoke-test-web-tools.sh
WEB_ACCEPTANCE_EXCLUSIVE=1 WEB_ACCEPTANCE_KIND=public ./smoke-test-web-tools.sh
```

Controlled and public-provider results are distinct. External `web_unavailable`
is reported as blocked coverage rather than relaxing policy or assertions.

## Kubernetes and real-provider boundaries

`smoke-test-k8s.sh`, `smoke-test-user-questions-k8s.sh` and
`smoke-test-web-tools-k8s.sh` use separate owned k3d/Helm resources. Network policy
acceptance requires real reachable IPv4/IPv6 controls; unit/Helm passes do not prove
CNI/SSRF enforcement. Browser traffic uses the actual Helm Ingress and loopback
NodePort path, not an SSE port-forward tunnel.

Real-provider Playwright scenarios require explicit gateway credentials/model and
`E2E_USE_REAL_LLM=true`. The mixed long-dialog scenario additionally opts into
questions/web and uses 26 turns with complete REST/reload history. Its prerequisites
and flags must be configured before rebuilding/recreating required components.
Paid provider availability is not inferred from a deterministic mock pass.

## Recovery and report privacy

Keep private journals on failed cleanup. Replay exactly the printed owned recovery
command using the original URLs/model identity and established history. Do not replace
closure proof with prefix SQL, broad Temporal terminate/search or a new namespace.
Kubernetes wrappers retain runtime/infrastructure when ownership cannot be proved.

Disable trace/video/screenshots/storage-state capture for auth-bearing acceptance.
Use the dedicated sanitized web/browser harness where required. Public reports
contain safe phase/status/counts, not credentials, raw HTTP, object payloads or
private marker/journal contents.

A fresh release report identifies its exact candidate hash, test commands and
PASS/BLOCKED/FAILED boundaries. Historical private runs are not a current release
pass. See [interactive coverage](testing/interactive-web-coverage.md) and
[data lifecycle coverage](testing/data-lifecycle.md).

## Manual acceptance commands and prerequisites

Run these sequentially against a disposable, built stack. Export the current stack
`APP_SECRET_KEY`; do not generate a replacement signing key for an existing deployment.
`./smoke-test-groups.sh` also needs the `master` realm administrator: export
`KEYCLOAK_ADMIN_PASSWORD` from `.env` (stacks initialized before generated Keycloak
passwords still use `admin`).
MCP/code need profiles `mcp` and `skills-exec` and a built `runner`/`skill-sandbox`.

| Command | Coverage / additional prerequisites |
| --- | --- |
| `./smoke-test.sh` | Ordinary chat/SSE with Keycloak and a runner |
| `./smoke-test-langfuse.sh` | Chat observability in the local Langfuse project |
| `./smoke-test-mcp.sh` | MCP SSE tools; deterministic overlay recommended |
| `./smoke-test-code-skill.sh` | Stateful code sandbox and bundle execution |
| `./smoke-test-groups.sh` | Group-based access to skills/models/documents |
| `./smoke-test-quota.sh` | Disposable DB/backend/gateway; existing Keycloak |
| `./smoke-test-organization-ui.sh` | Both UI audiences, models/analytics/revoke; temporary ports8000/8001 |
| `./smoke-test-multi-agent.sh` | Sequential/parallel DAG, revoke/quota/cancel and owned cleanup |
| `./smoke-test-knowledge-ui.sh` | Real UI and MinIO/Temporal/Milvus; wrapper starts RAG profile |
| `./smoke-test-workflow-ui.sh` | Real UI workflow authoring, deterministic gateway |
| `./smoke-test-workflow-templates-ui.sh` | Template versions/copies/grants/HITL/archive |
| `./smoke-test-workflow-routing-ui.sh` | Auto selection, malformed/inaccessible fallback, explicit picker priority |
| `./smoke-test-workflow-branching-ui.sh` | Node failure, conditional edges, AND-join, skip cascade |
| `./smoke-test-workflow-hitl-ui.sh` | Reject/cancel and real65-second dedicated test deadline |
| `./smoke-test-workflow-loops.sh` | API-only loop cap/fallthrough/cancel and previous-iteration context |
| `./smoke-test-archive-images.sh` | Disposable lifecycle project: data-worker archives/restores an old conversation with images; scoped image IAM |
| `./smoke-test-hermes-ui.sh` | Real UIs with the Hermes overlay and the deterministic gateway: the Admin UI picks the Hermes model; a member chooses Hermes, sees its tool cards and keeps it chosen after a reload, answers its question card, clears its memory in settings and sees a skill draft; the administrator reads and approves the draft, and the next turn's sandbox has the skill; Hermes attaches a chart and a CSV it made in its terminal (shown, downloaded, kept after a reload) and is refused a platform secret; the member gives Hermes two workbooks, which reach `/workspace/inputs/`, and gets back a pivot workbook and a deck with the pivot's totals. With `HERMES_E2E_REAL_LLM=true` it runs `hermes.real-llm.spec.ts` through the stack's gateway on `E2E_HERMES_MODEL` (default `deepseek-flash-free`; native tool calling required): a hash only the terminal can produce, a matplotlib chart and its CSV attached, a pivot workbook and a deck built from two uploaded workbooks (the answer is the pivot's total), a question card, memory and a skill draft. Not at the same time as the sandbox acceptance (one egress guard) |
| `./smoke-test-hermes-sandbox.sh` | Disposable project: skill-executor creates Hermes sandboxes from this checkout; a run reaches the real backend route through hermes-egress and is billed; no capabilities, non-root, read-only root, no secrets in metadata; sandboxes cannot reach each other or the host; re-adoption after an executor restart, guard reconnection, and nothing left after close or idle expiry |
| `./smoke-test-hermes-egress.sh` | Disposable project with the real `docker-compose.hermes.yml`: a probe on the Hermes sandbox network has no DNS or routes of its own, reaches only the completion path, and gets internet egress per mode (`none`, `packages` with `pip`, `public` with `web.access`); a closed session cuts an open tunnel. Needs internet access |
| `./smoke-test-authorization-ui.sh` | Five resource types, live revoke and both audiences |
| `./smoke-test-images-ui.sh` | Admin image and vision models, chat generation, editing and image attachments, reload; deterministic gateway |
| `./smoke-test-auth-lifecycle.sh` | UI logout/refresh/recovery and natural access expiry (up to15minutes) |
| `./smoke-test-k8s.sh` | Fresh owned k3d/Helm install/upgrade, runner/sandbox/RBAC |
| `./smoke-test-user-questions-k8s.sh` | Fresh owned questions Ingress/Pod UID lifecycle |
| `./smoke-test-web-tools-k8s.sh` | Fresh dual-stack CNI/SSRF controls; required web secrets/exclusive flag |

`./seed-skills.sh` is deliberate manual development setup, not a test; acceptance
removes its own fixtures and leaves no persistent seed skills.

Workflow C5 wrappers share `E2E_BUSINESS_UI_URL`, `E2E_ADMIN_UI_URL`,
`E2E_BUSINESS_BACKEND_URL`, `E2E_ADMIN_BACKEND_URL`, `E2E_KEYCLOAK_URL` and
`E2E_POSTGRES_CONTAINER`. Their marker/state/model variables are wrapper-owned;
unmanaged loop mode and legacy overrides are rejected. Do not launch standard
wrappers against a differently named Compose stack: they verify their expected
service identities. The recovery CLI additionally accepts `E2E_BACKEND_CONTAINER`
for an explicitly identified backend and verifies matching Compose project/service
labels before executing exact Temporal RPCs.

The knowledge wrapper may need up to15minutes to download/warm the reranker on
fresh cache; `RERANKER_READY_ATTEMPTS` controls its polling bound. The auth lifecycle
wrapper changes only its own browser SSO session, never realm/client/membership
settings or logout-all, and does not prove natural refresh-token expiry.

For deterministic overlay setup, build runner/sandbox before applying it:

```bash
export APP_SECRET_KEY=<current-stack-secret>
docker compose -f docker-compose.yml -f docker-compose.e2e.yml --profile build build runner skill-sandbox
docker compose -f docker-compose.yml -f docker-compose.e2e.yml --profile mcp --profile skills-exec build backend backend-admin tool-worker mcp-everything mcp-fetch skill-executor test-gateway
docker compose -f docker-compose.yml -f docker-compose.e2e.yml --profile mcp --profile skills-exec up -d --no-build backend backend-admin tool-worker mcp-everything mcp-fetch skill-executor
./smoke-test-mcp.sh
./smoke-test-code-skill.sh
docker compose -f docker-compose.yml -f docker-compose.e2e.yml rm -sf test-gateway
docker compose -f docker-compose.yml up -d --force-recreate backend
```

The repository harness requires synthetic `APP_SECRET_KEY` and `APP_DATABASE_URL`
environment values just as CI does; its optional database/container tests require
their documented separate prerequisites. Keep logs private because third-party
assertion diagnostics may include environment values.
