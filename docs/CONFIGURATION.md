# Configuration

Start with tracked `.env.example` and persistent `scripts/bootstrap-dev-env.sh`.
Actual `.env` files, credentials and generated provider configuration are ignored
and excluded from public exports. The root env serves Compose; each Python service
uses its own settings prefix. Do not copy every root variable into a service dotenv.

## API and identity

| Setting | Meaning |
|---|---|
| `APP_SECRET_KEY` | Session signing secret; generate a strong persistent value |
| `APP_DEBUG` | Development/mock/docs behavior; false for configured production |
| `APP_DATABASE_URL` | Scoped runtime database connection |
| `APP_MIGRATION_DATABASE_URL` | Separate privileged migration connection |
| `APP_DATABASE_ROLE_PASSWORD` | Runtime role creation/configuration password |
| `APP_REDIS_URL` | Redis connection, including configured auth |
| `APP_KEYCLOAK_URL`, realm/client/issuer settings | Validated identity origin/audience |
| `APP_JWT_CLOCK_SKEW_SECONDS` | Tolerated Keycloak/API clock difference for token `iat`/`nbf` (default 30); expiry stays exact |
| `PUBLIC_HOST` | Compose browser-visible API/Keycloak/UI host |
| `PUBLIC_BIND_ADDRESS` | Host interface for browser-facing Compose ports; `127.0.0.1` by default |
| `KEYCLOAK_ADMIN_PASSWORD` | Generated `master` realm administrator password, applied on first Keycloak start |
| `PUBLIC_DOMAIN`, `ACME_EMAIL` | Base domain and Let's Encrypt contact for `docker-compose.proxy.yml` |
| `TEMPORAL_UI_BASIC_AUTH_USER`, `TEMPORAL_UI_BASIC_AUTH_PASSWORD` | Proxy basic-auth credentials for Temporal UI |
| `ACME_CA_SERVER` | Optional ACME directory, for example the Let's Encrypt staging endpoint |

The business/admin planes have different Keycloak client audiences. Changing URLs
requires compatible realm redirect origins and CORS, not disabling signature,
issuer/audience or date validation. The proxy overlay sets a fixed Keycloak hostname
(`https://auth.<PUBLIC_DOMAIN>`) and the matching backend issuer; the realm import
reads `AURUMAI_CHAT_ORIGIN`/`AURUMAI_ADMIN_ORIGIN` for the extra client origins. Replace documented development users for
production. The database is `aurum_ai` with `aurum_ai_*` roles; the realm is `aurum-ai`.

Bootstrap-generated passwords are URL-safe. When supplying custom database values,
use valid connection-URI encoding or an explicit connection URL. Persistent storage
credentials must remain consistent with the initialized service; recreating an env
file does not rotate an existing database/MinIO account automatically.

## Gateway and evaluation

`LLM_GATEWAY_URL`/`APP_LLM_GATEWAY_URL` identify the external gateway base without a
final `/v1`. `GATEWAY_API_KEY` is supplied by the operator. TLS verification is on;
corporate CA configuration uses the existing gateway CA/verification settings.
The runner uses the API proxy, not direct provider credentials.

The Bifrost wrapper preserves persistent app secrets. OpenRouter requires
`OPENROUTER_API_KEY`; the shipped Bifrost configuration contains only an
environment reference to that key.

`EVALUATOR_ENABLED=true` additionally needs an explicit `EVALUATOR_MODEL`. New
workflows skip evaluator creation when its model is empty. Its replay marker is
permanent; do not remove old workflow markers during a branding/configuration edit.
Real-provider E2E requires `E2E_USE_REAL_LLM=true` and `E2E_LLM_MODEL` explicitly.

`RAG_API_SEARCH_EMBEDDING_TIMEOUT_SECONDS` (default 10) bounds how long knowledge
search waits for the query embedding. A CPU-only embedding model under memory
pressure can exceed it on the first query after idling, which surfaces as a failed
search; raise it on such hosts.

## Frontend runtime values

Both UIs read `RUNTIME_*` on the server for each request:

- `RUNTIME_API_URL`, including the application `/v1` path.
- `RUNTIME_KEYCLOAK_URL`, `RUNTIME_KEYCLOAK_REALM`, `RUNTIME_KEYCLOAK_CLIENT_ID`.
- `RUNTIME_ADMIN_URL` for chat UI; `RUNTIME_APP_URL` for admin UI.
- `RUNTIME_SOURCE_CODE_URL`, pointing to the actual source of this deployment.

The interface language is chosen per request: an explicit `NEXT_LOCALE` cookie
(`en` or `ru`, set by the language switcher) wins, otherwise the first supported
`Accept-Language` entry, otherwise English. URLs carry no locale prefix.

These are not `NEXT_PUBLIC_*` build inputs. The same image can be deployed behind
other browser-visible hosts with runtime settings. Source URLs are HTTP(S) without
embedded URL credentials; empty/invalid source configuration does not invent a
repository. Both applications ship a local copy of the project LICENSE.

## Runner and workflows

`APP_RUNNER_RUNTIME=docker|kubernetes` and `SKILL_EXECUTOR_RUNTIME` select runtime
at startup. Compose can use a warm pool; Kubernetes uses cold runners and pool size
zero. Docker network/image settings must match the selected deployment. Kubernetes
runner and sandbox namespaces are distinct and require appropriate RBAC, network
policy and RuntimeClass/node PID enforcement.

Workflow ceilings are `APP_WORKFLOW_MAX_NODES=20` and
`APP_WORKFLOW_MAX_PARALLELISM=4`. Diagnostic retention defaults to 30 days and is
independent of data archiving. Conversation/organization/agent temperatures are
nullable overrides resolved server-side; a missing override inherits its configured
parent/default, not a hardcoded model-specific value.

## Questions and web

`USER_QUESTIONS_ENABLED` in Compose supplies server-owned
`APP_USER_QUESTIONS_ENABLED`; the Helm equivalent is `backend.userQuestionsEnabled`.
It is default-off. Disabling new reservations leaves existing question lifecycle
operations available. Backend, runner and UI must be compatible before enabling.

Web additionally needs the overlay/profile, `WEB_TOOLS_SERVICE_SECRET`,
`SEARXNG_SECRET`, `web.access` grants and enforcement acceptance. The backend switch
is separate from deploying the web boundary. `WEB_EGRESS_FORBIDDEN_CIDRS` adds
operator deny ranges to application, guard and Helm policies. Do not weaken the
required ranges or treat an unavailable positive control as a passed deny test.

## Identity and data lifecycle

`APP_IDENTITY_SYNC_ENABLED=false` is the default. Install/verify the signed complete-
groups mapper on both clients, apply migrations and start the maintenance worker/
egress guard before enabling. `APP_IDENTITY_SYNC_WAIT_SECONDS` defaults to 10;
changed identity admission is bounded during worker outages.

New archive jobs are default-off. `APP_ARCHIVE_RETENTION_DAYS=180` and
`APP_ARCHIVE_SWEEP_INTERVAL_SECONDS=86400` control eligibility and sweep timing;
`APP_ARCHIVE_BATCH_SIZE` bounds a batch. Use separate lifecycle database accounts,
archive S3 credentials and private RAG service secret/endpoint. Normal API/RAG
accounts must not receive lifecycle/schema-owner rights or archive bucket access.

Observer terminal receipts and partition maintenance continue independently of
new-archive enablement. See [the archive runbook](operations/data-archive.md) for
roles, bounds, dry-run/apply, export and exact recovery.

## Observability and secrets

Langfuse is operator-configured. Capture of full prompts/documents is a deliberate
privacy decision; disabling telemetry is separate from full-content capture.
Generate root/auth/encryption secrets for the local services and restrict their
ports. Never publish a complete env dump or browser trace containing tokens.

Helm secret ownership/CIDRs and all deployment-specific prerequisites are described
in [the chart guide](../charts/aurum-ai/README.md). No source release can supply
an operator's real cloud accounts, private certificates or production credentials.

Backend automatically reads only `business-backend/.env`, relative to the service source. A root `.env` belongs to Docker Compose and is not implicitly loaded by API imports or harness commands. An explicit `Settings(_env_file=...)` still validates unknown settings strictly.

Bootstrap accepts single-line dotenv assignments, including whitespace, optional `export`, quoted keys/values and inline comments. It preserves configured values byte-for-byte, detects duplicate required keys after normalization, and fills quoted empty values. Unsupported or multiline quoted syntax fails before replacing the file; review those configurations manually. The parser uses the existing Bash/OpenSSL/awk prerequisites and never executes env content.
