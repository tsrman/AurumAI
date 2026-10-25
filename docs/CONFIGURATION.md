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

In Admin UI, select a default model under Models. A model marked as requiring
organization access can be selected for configuration; use the grant editor below
to give the organization read access, apply the grants, then save the default.
Selecting a default never grants permission by itself.

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

## Images

`IMAGES_ENABLED=false` (`APP_IMAGES_ENABLED`) is the operator switch for the
`generate_image` and `edit_image` chat tools. A turn offers them only when the
switch is on, the user has the `images.generate` capability (Admin UI → Groups)
and the organization has chosen a model in Admin UI → Models → Images. The
generation and editing models are separate settings; either can stay off.

The backend calls an OpenAI-compatible Images API (`/v1/images/generations` and
`/v1/images/edits`). `IMAGES_API_URL` and `IMAGES_API_KEY` default to the chat
gateway and its key; `IMAGES_TIMEOUT_SECONDS` bounds one provider call. Only inline
`b64_json` results are accepted, never provider URLs, and the bytes must be PNG,
JPEG or WebP. Images are stored in the platform bucket under
`conversation-images/<org>/<conversation>/` and served only to the conversation
owner. Each turn may create at most four images; image-enabled turns list the
conversation's eight newest images to the model so a later turn can edit them.
Conversations with images are not archived until the archive format carries image
bytes.

The same switch also enables image attachments. In Admin UI → Models → Images an
administrator marks the chat models that accept image input; the chat composer
offers PNG, JPEG and WebP attachments only while such a model is selected, and the
backend rejects images sent to any other model. A message carries at most four
images of at most `IMAGES_UPLOAD_MAX_BYTES` (10 MB) each; uploads need only
`chat.send`. The runner forwards image ids, never bytes: the chat completions proxy
inlines the four newest images of the request as base64 `image_url` parts, and
replaces older images, or all of them when the selected model is not marked, with a
short text note. Attached images also appear in the image list a later turn can edit. Removing an
attachment before sending deletes it; an upload left unsent (for example when the tab
closes) stays with the conversation, unused, and like any conversation image keeps
it from being archived.
Workflows use images too. Their node agents get `generate_image` and `edit_image`
under the same switch, capability and organization models (supervisors never do);
a node can edit an image an earlier node created, and the images of the nodes the
final answer is built from are shown under it. Image calls of a workflow are
billed to its run, phase and node, and the per-turn limit of four images is shared
by all of its nodes. Attached images go to the supervisor and to every node; a
workflow accepts them whatever the composer model is, and each agent receives them
only if its own model is marked as accepting images. Because every agent call sends
them again, attachments cost more in a workflow than in a chat turn.

Image tools share the organization's monthly quota with chat. Each Images API attempt
reserves one request before the call (an exhausted quota fails the tool with
`image_quota_exceeded`, which the model is told about), is counted once the provider
answers, and adds the tokens the provider reports in its `usage` block to the token
quota; an attempt the provider never answered (a timeout, a refusal, an outage) gives
its reservation back. A provider that reports no usage leaves the call counted as a
request without token data. Usage events record the call as `image_generate` or
`image_edit`, and Admin UI → Analytics shows the number of images per model and per
user next to requests and tokens. Image tokens of attachments sent to vision models are
part of the chat completion's own prompt tokens.

## Hermes Agent

Hermes Agent is being integrated in phases (see [the design](HERMES_AGENT.md)).
`APP_HERMES_ENABLED=false` is its operator switch; while it is off, the Hermes routes
answer 404. Users also need the `agents.hermes` capability (Admin UI → Groups), which
provisioning never grants. The first phase adds only the backend side a Hermes
sandbox will call; nothing starts a sandbox yet. Compose passes the switch only with
the `docker-compose.hermes.yml` overlay; the Helm chart does not pass it yet.

- `POST /v1/hermes/chat/completions` is an OpenAI-compatible completion route with
  native tools: `tools`, `tool_choice`, assistant `tool_calls`, `tool` messages and
  inline `data:image/png|jpeg|webp;base64` images (never remote image URLs). The
  gateway's chunks, tool call deltas included, are streamed unchanged. The
  configured `LLM_GATEWAY_URL` must therefore forward native tools; the runner's own
  route does not need that. Model grants, the vision model list and the monthly quota
  apply as on the runner route, and requests count against the user's ordinary rate
  limit. `APP_HERMES_MAX_TOOLS` (128) and `APP_HERMES_MAX_MESSAGES` (1000) bound one
  request.
- The route accepts only a Hermes session token, and only while its session is open,
  unexpired and bound to an active turn. A runner manages the session of its own
  conversation through internal routes: `POST /v1/internal/hermes/sessions` opens it
  (or rotates the token of the open one, so earlier tokens stop working) and returns
  a token; `PUT .../sessions/{id}/turn` binds it to the conversation's active turn,
  with the turn's workflow run, phase and node when the turn runs a workflow;
  `DELETE .../sessions/{id}/turn` unbinds it and `POST .../sessions/{id}/close` closes
  it. Opening and binding need the Hermes capabilities, unbinding and closing never
  do. Completions are billed to the bound turn's workflow correlation, never to
  anything in the request. A token holds identity only and has its own audience that
  no other route accepts; a session and its tokens end after
  `APP_HERMES_SESSION_MAX_LIFETIME_SECONDS` (86400) even if nothing closes them.

### Hermes sandbox egress

`docker-compose.hermes.yml` (profile `hermes`) adds `hermes-egress`, the only way
out of a Hermes sandbox. Sandboxes join `aurum-ai-hermes-sandbox`
(`HERMES_SANDBOX_SUBNET`, default `172.31.250.0/24`), an internal network without
NAT or external DNS where the egress guard is the only platform member. The backend
never joins it. Sandboxes share this segment; sandbox isolation from one another
arrives with the sandboxes themselves.

- **Platform path.** `hermes-egress:8080` forwards exactly
  `POST /v1/hermes/chat/completions`, one request per connection with a
  `Content-Length` body.
- **Internet.** `hermes-egress:3128` is a `CONNECT`-only proxy. A sandbox sets
  `HTTPS_PROXY=http://hermes:<session token>@hermes-egress:3128`. For each tunnel
  the proxy asks the backend (`POST /v1/internal/hermes/egress/authorize`,
  authenticated with `HERMES_EGRESS_SERVICE_SECRET`) whether the session is open and
  bound to an active turn, and which mode applies. It re-checks open tunnels every
  `HERMES_EGRESS_RECHECK_SECONDS` (30) and closes those no longer allowed, and also
  those it could not re-check three times in a row (backend unavailable).

  | Mode | Reachable |
  |---|---|
  | `none` | nothing |
  | `packages` | `HERMES_EGRESS_PACKAGE_HOSTS` on 443, and the TLS server name must match |
  | `public` | any public host on 80 or 443; needs the user's `web.access` |

  `HERMES_EGRESS_MAX_MODE` (default `packages`) caps the backend, the proxy and the
  guard's public ports at once. Organizations choose within it through
  `GET`/`PUT /v1/admin/settings/hermes` (`egressMode`, default `packages`); the
  Admin UI control comes with the Hermes chat agent.
- **What stays closed.** Private, loopback, link-local and platform ranges and
  `WEB_EGRESS_FORBIDDEN_CIDRS` are refused after resolution, and the tunnel connects
  to the checked address. An exact `host:port` in
  `HERMES_EGRESS_INTERNAL_PACKAGE_HOSTS` is the only private exception, meant for an
  internal package mirror.
- **Limits.** `HERMES_EGRESS_MAX_CONNECTIONS` (16 per session),
  `HERMES_EGRESS_MAX_BYTES_PER_TURN` (2 GiB) and `HERMES_EGRESS_IDLE_TIMEOUT_SECONDS`
  (300). Counters are kept in memory by each hermes-egress replica and reset when
  it restarts.
- **Journal.** Every decision and closed tunnel is a JSON line in the hermes-egress
  log: session, turn, host, port, mode, outcome and bytes, never credentials or
  traffic. The backend's rate limit and audit journal skip the per-connection check.

The Helm chart does not deploy hermes-egress yet.

### Hermes sandboxes

With the overlay, skill-executor runs Hermes sandboxes (Docker runtime only;
Docker 28 or newer, for isolated-gateway internal networks). Each sandbox gets its
own `/28` network from `HERMES_SANDBOX_POOL` (default `172.31.0.0/16`, the guard's
sandbox CIDR), a workspace volume and the `aurum-ai-hermes-sandbox` image, built
with `docker compose -f docker-compose.yml -f docker-compose.hermes.yml --profile
build build hermes-sandbox`. skill-executor settings (`SKILL_EXECUTOR_` prefix):

| Setting | Default |
|---|---|
| `HERMES_ENABLED` | `false` (the overlay passes `HERMES_ENABLED`) |
| `HERMES_CPUS`, `HERMES_MEMORY_BYTES`, `HERMES_PIDS_LIMIT`, `HERMES_TMP_MB` | 2, 2 GiB, 512, 256 |
| `HERMES_IDLE_TTL_SECONDS`, `HERMES_MAX_LIFETIME_SECONDS` | 1800, 86400 |
| `HERMES_START_TIMEOUT_SECONDS` | 60 |

A restarted skill-executor re-adopts live sandboxes from their Docker labels and
reconnects a recreated guard; its reaper removes sandboxes past their idle or
absolute lifetime and networks or volumes left without a sandbox. Workspace
volumes have no size limit with Docker's local volume driver.

## Identity and data lifecycle

Groups synchronized from Keycloak are managed in Keycloak. Admin UI disables local
deletion for these groups, and the API rejects it: deleting only the local copy
would allow synchronization to recreate it with a new identity. Groups created
locally and not yet synchronized can still be deleted in Admin UI.
Group membership always comes from the Keycloak `groups` claim at sign-in: a group
created locally with the same name as a Keycloak group picks up its members, and
the Members tab of the group dialog shows them read-only.

`APP_IDENTITY_SYNC_ENABLED=false` is the default. Install/verify the signed complete-
groups mapper on both clients, apply migrations and start the maintenance worker/
egress guard before enabling. `APP_IDENTITY_SYNC_WAIT_SECONDS` defaults to 10;
changed identity admission is bounded during worker outages.

New archive jobs are default-off. `APP_ARCHIVE_RETENTION_DAYS=180` and
`APP_ARCHIVE_SWEEP_INTERVAL_SECONDS=86400` control eligibility and sweep timing;
`APP_ARCHIVE_BATCH_SIZE` bounds a batch. Use separate lifecycle database accounts,
archive S3 credentials and private RAG service secret/endpoint. Normal API/RAG
accounts must not receive lifecycle/schema-owner rights or archive bucket access.

Conversations with images are archived only when the data worker has
`APP_ARCHIVE_IMAGES_S3_ENDPOINT`, `APP_ARCHIVE_IMAGES_S3_ACCESS_KEY` and
`APP_ARCHIVE_IMAGES_S3_SECRET_KEY` (bucket `APP_ARCHIVE_IMAGES_S3_BUCKET`, default
`aurum-ai-business`): separate credentials for the platform bucket, scoped to the
`conversation-images/*` prefix with Get, Put and Delete. Without them such
conversations stay live with the blocked reason `image_archive_not_configured`.

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
