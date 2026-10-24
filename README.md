# AurumAI

A self-hosted multi-agent AI platform with durable conversations, tools, knowledge
bases and configurable workflows. AurumAI combines a FastAPI API, two Next.js
applications, Keycloak identity and Temporal orchestration. Each conversation runs
in an isolated Docker container or Kubernetes Pod.

Project-owned source is licensed under **AGPL-3.0-only**. See [LICENSE](LICENSE),
[COPYRIGHT](COPYRIGHT) and [third-party notices](THIRD_PARTY_NOTICES.md).
This is a development source release; production deployment requires explicit
infrastructure, credential and isolation configuration.

## Start locally

Requirements: Docker with Compose v2, Bash, OpenSSL, and sufficient memory/disk for
container builds. Python 3.12/uv and Node 22/npm are needed for source development
and tests. Use Linux, macOS or WSL2 with a working Docker installation.

```bash
./scripts/bootstrap-dev-env.sh
./install-platform.sh          # review the installation commands
./install-platform.sh --apply  # start the platform and install starter tools
```

Bootstrap writes persistent generated secrets into ignored `.env` with private
permissions. Repeating it preserves configured values. The installer builds the
runner and bounded code sandbox, starts the platform, then installs offline CSV
and JSON skills plus SSE MCP calculator and text round-trip tools. It preserves
existing tool definitions and revoked grants. See [starter tools](docs/STARTER_TOOLS.md)
for explicit existing-organization targets and deployment configuration.

For a minimal stack without starter tools, build the runner with
`docker compose --profile build build runner`, then run `docker compose up -d --build`.

Open the chat at `http://localhost:3000` and administration at
`http://localhost:3100`. Development accounts are `user`/`user` and `admin`/`admin`;
`user2`/`user2` and `admin2`/`admin2` belong to a second test organization.
The Keycloak console administrator (`admin` in the `master` realm) uses the
generated `KEYCLOAK_ADMIN_PASSWORD` from `.env`; Keycloak applies it only when it
initializes an empty database. All browser-facing ports listen on `127.0.0.1` by
default.
The base development setup uses the existing local mock LLM mode. Real providers
are optional and consume operator-supplied credentials.

[QUICKSTART](QUICKSTART.md) covers readiness, login, the first chat, optional
profiles and troubleshooting. Configure real credentials, intended origins and
TLS before exposing any service outside a trusted development environment.

## Components

| Component | Responsibility | Source |
|---|---|---|
| Business API | Chat, conversations, user settings, runtime admission | `business-backend/`, port 8000 |
| Admin API | Skills, grants, agents, workflow definitions/templates | Same backend, port 8001 |
| Chat UI | Conversations, history, knowledge, questions, workflow progress | `business-ui/`, port 3000 |
| Admin UI | Users and groups, agents, skills, workflows, knowledge access, models and analytics | `admin-ui/`, port 3100 |
| Conversation runner | Per-conversation Temporal worker and agent/workflow execution | `conversation-runner/` |
| Tool worker | Skill dispatch and tenant MCP over SSE | `tool-worker/` |
| Skill executor/sandbox | Isolated multi-round code-skill execution | `skill-executor/`, `skill-sandbox/` |
| RAG API/worker | Document storage, parsing, indexing and hybrid search | `rag-api/`, `rag-worker/` |
| Web tools | Bounded search/read with URL and outbound-network policy | `web-tools/` |
| Deployment | Compose for development, Helm for Kubernetes | `docker-compose.yml`, `charts/aurum-ai/` |

The data plane uses PostgreSQL, Redis, MinIO and optional Milvus. Keycloak handles
identity; Temporal coordinates durable execution. Langfuse is optional
observability infrastructure. Dependencies, external images and downloaded models
retain their own licenses.

## Capabilities

- English and Russian interfaces: the browser language selects one (English
  otherwise) and the language switcher remembers the choice in the `NEXT_LOCALE`
  cookie. Agent prompts and model output are not translated.
- Streaming AI chat with complete paginated history and reload recovery.
- Organization-scoped authorization through capabilities and resource grants,
  live revocation, separate browser audiences and identity-only runner sessions.
- MCP tools and uploaded code-skill bundles in bounded isolated sandboxes.
- Knowledge bases and chat attachments with document parsing and hybrid retrieval.
- Versioned workflow definitions/templates, explicit selection or opt-in routing,
  parallel DAG nodes, conditional branches, human approval and bounded loops.
- Loop iteration `N+1` receives all terminal results from **only iteration `N`**;
  earlier snapshots are replaced rather than accumulated.
- Durable `ask_user` questions and bounded web search/read/citations, both default-off.
- Optional background Keycloak synchronization, monthly payload partitions, and
  verified MinIO/Parquet archive/export/restore with operator-only recovery.

## Configure a real model

Any supported OpenAI-compatible gateway can be configured in `.env`:

```dotenv
LLM_GATEWAY_URL=https://gateway.example.org
GATEWAY_API_KEY=operator-supplied-key
```

The gateway base URL excludes the final `/v1`; the backend constructs the API path.
Recreate both API planes after changing gateway settings. The runner always calls
the backend proxy with its own session token, preserving authorization and quota.

The optional local Bifrost overlay supports OpenRouter:

```bash
export OPENROUTER_API_KEY=operator-supplied-key
./start-with-llm-gateway.sh
```

The provider key remains an environment reference in `bifrost/config.json`.
No personal cloud account or evaluator model is a shipped default.

Evaluation is disabled by default in deployment examples. To enable it, configure
both `EVALUATOR_ENABLED=true` and an explicit `EVALUATOR_MODEL` available through
the gateway. An empty model never launches a new evaluation child.

Configure multi-agent task graphs with the [workflow guide](docs/WORKFLOWS.md),
including three complete examples, agent mapping, validation, access and chat execution.

## Optional profiles and rollout gates

| Feature | Setup |
|---|---|
| Starter MCP utilities | Included by `./install-platform.sh --apply`; uses only `mcp-everything` |
| Unrestricted MCP fetch fixture | Opt-in test profile only; `docker compose --profile mcp up -d mcp-fetch` |
| Code skills | Build `skill-sandbox` with profile `build`; start `skill-executor` with profile `skills-exec` |
| RAG | `docker compose --profile rag up -d --build` |
| Durable questions | Enable server-owned `USER_QUESTIONS_ENABLED`; deploy compatible backend, runner and UI |
| Web search/read | Use `docker-compose.web.yml`, profile `web`, service/search secrets and operator enforcement checks |
| Identity synchronization | Profile `maintenance`, identity worker/egress guard, compatible realm claim mapper |
| Archiving | Separate lifecycle roles/S3 accounts/private RAG endpoint; observer receipts before enabling new jobs |

Reference MCP servers are development fixtures. In particular, unrestricted
fetch and operator-global stdio do not become safe tenant tools merely because
they work in a local demo. Tenant MCP uses SSE; tenant code uses the sandbox.

## Deployment boundaries

Compose gives the trusted API host-root-equivalent Docker-socket authority to
create conversation containers. Never expose that backend to untrusted clients.
Helm uses namespace-scoped Kubernetes RBAC and network policies instead, with
separate application, runner and sandbox namespaces.

Technical identifiers use the `aurum-ai` form (Keycloak realm, Helm chart,
Compose project, Kubernetes labels and buckets) and `aurum_ai` for the database and
its roles. A clean public repository starts with a new source history; private
development logs, incident journals and design caches are excluded.

## Publish through a reverse proxy

`docker-compose.proxy.yml` adds Traefik with Let's Encrypt certificates and
publishes only ports 80/443. All other ports stay on `PUBLIC_BIND_ADDRESS`
(loopback by default). Create DNS records for these subdomains of your domain
and point them at the host:

| Host | Service |
|---|---|
| `chat.<domain>` | Chat UI |
| `admin.<domain>` | Admin UI |
| `api.<domain>`, `admin-api.<domain>` | Business and admin APIs |
| `auth.<domain>` | Keycloak, including its admin console |
| `temporal.<domain>` | Temporal UI behind proxy basic auth |
| `langfuse.<domain>` | Langfuse (when started) |

Set `PUBLIC_DOMAIN` and `ACME_EMAIL` in `.env`, run
`./scripts/bootstrap-dev-env.sh` to generate `TEMPORAL_UI_BASIC_AUTH_PASSWORD`,
then start the stack with both files:

```bash
docker compose -f docker-compose.yml -f docker-compose.proxy.yml up -d --build
```

The overlay switches browser URLs, CORS origins and the Keycloak issuer to
`https://` subdomains. Realm redirect origins for the proxy hosts are applied on
the first realm import. An existing Keycloak database keeps its clients, so add
`https://chat.<domain>/*` and `https://admin.<domain>/*` to the `business-ui` and
`admin-ui` clients in the Keycloak console. Use
`ACME_CA_SERVER=https://acme-staging-v02.api.letsencrypt.org/directory` while
testing DNS to avoid production rate limits. Replace the development accounts and
set `APP_DEBUG=false` with real secrets before exposing the stack to untrusted users.

Both UI applications expose `/about` and a local `/LICENSE`. Set
`RUNTIME_SOURCE_CODE_URL` to the actual source of a hosted deployment. This is
runtime configuration, so updating it does not require rebuilding the UI.

## Documentation

- [Quickstart](QUICKSTART.md)
- [Architecture and interface boundaries](docs/ARCHITECTURE.md)
- [Configuration](docs/CONFIGURATION.md)
- [Kubernetes/Helm](charts/aurum-ai/README.md)
- [Air-gapped deployment](docs/AIR_GAPPED.md)
- [Data archive/export/restore](docs/operations/data-archive.md)
- [Testing and coverage](docs/TESTING.md)
- [Roadmap](ROADMAP.md), [changelog](CHANGELOG.md), [release preparation](docs/RELEASE.md)
- [Contributing](CONTRIBUTING.md), [security](SECURITY.md), [support](SUPPORT.md)

## Stop

```bash
docker compose down
```

Data volumes are retained. `docker compose down -v` also deletes volume data;
use it only when intentionally discarding a disposable development installation.

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
