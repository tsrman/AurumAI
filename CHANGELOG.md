# Changelog

## v0.0.8 (2026-11-08)

A database pass for many simultaneous users (two migrations, `20261110` and `20261111`) and the keycloak-js, TypeScript and MCP SDK major upgrades.

### Performance

- **Reading a conversation's messages no longer probes every month.** `messages` is partitioned
  by month and a conversation-scoped query carried no hint about which months, so each read touched
  every partition ever created: planning alone took 8 ms at 46 partitions and grows with every month.
  The history read now starts at the conversation's creation (a message is never older than its
  conversation) and joins its rows by the whole primary key: 0.2 ms to plan and 0.07 ms to run in the
  same test.
- **Deleting a conversation no longer scans five tables.** `interaction_turns`,
  `conversation_executions`, `hermes_sessions`, `archive_jobs` and `file_metadata` had no index for
  the lookup the delete makes (migration `20261111_cascade_lookup_indexes`). The same migration drops
  six single-column `org_id` indexes that another index on the table already covered
  (`interaction_turns`, `conversation_executions`, `hermes_sessions`, `tool_operations`,
  `conversations`, `audit_logs`), which lowers the cost of every write to them. Index builds block
  writes to their table; upgrade a large installation in a quiet period.
- **The audit row is written after the response.** A mutating request used to wait for a second pooled
  connection, an `INSERT` and a commit before the client got anything, including before a chat stream
  started. The write now runs in the background, at most 200 at a time (beyond that the request waits
  for its own write; no row is dropped), and is finished at shutdown.
- **The Compose PostgreSQL starts with room for the stack.** `max_connections=200`,
  `shared_buffers=256MB`, `effective_cache_size=1GB` (PostgreSQL's defaults are 100 and 128MB), set with
  `POSTGRES_MAX_CONNECTIONS`, `POSTGRES_SHARED_BUFFERS` and `POSTGRES_EFFECTIVE_CACHE_SIZE`. Recreate the
  `postgres` container to apply them.
- **An organization's requests no longer queue on one row for long.** Every model request of an
  organization updates the same monthly counter row, and the row stayed locked while the transaction
  made several more round trips and then waited for the disk flush. The counter is now touched last,
  by one statement that both checks the limits and counts the request (the limit stays exact), and
  quota transactions do not wait for the flush. Measured on one organization with 24 parallel
  connections: 158 → 685 requests per second, against about 1 000 for 24 separate organizations. The
  one trade-off: if PostgreSQL itself crashes, the usage counts of the last fraction of a second
  (about half a second) can be lost; the data is not corrupted.
- **An open chat no longer holds a database connection.** The turn event stream
  (`GET /v1/conversations/{id}/turns/{id}/events`) kept the request's session, and so a pooled
  connection "idle in transaction", for as long as the stream was open, up to two minutes. One
  stream per active chat exhausted the 60-connection pool of a process. The stream now ends the
  request's transaction before it starts and reads each later snapshot with a short-lived session.
- **The connection pool is configurable.** `APP_DB_POOL_SIZE`, `APP_DB_MAX_OVERFLOW` and
  `APP_DB_POOL_TIMEOUT` (defaults unchanged: 20, 40 and 30 seconds); the operations guide now has the
  connection budget and how to see who holds connections.
- **The conversation list no longer reads every conversation.** The sidebar's query (one person's
  conversations, newest activity first) had no index starting with `(org_id, user_id)`, so it scanned
  every partition: 34 ms with 300 000 conversations and growing with them. The new index
  `ix_conversations_user_activity` (migration `20261110_conversation_list_index`) answers it in
  0.4 ms regardless of the table's size. Creating an index on a partitioned table blocks writes to
  `conversations` while it builds; upgrade a large installation in a quiet period.
- **Signing in is lighter for groups.** Every authenticated request rewrote `groups.last_synced_at` for
  each of the person's groups, so every member of a group queued on the same row. The timestamp is now
  refreshed at most every 15 minutes, and a request that changes nothing no longer commits or re-reads
  the user.

### Changed

- **keycloak-js 26 and TypeScript 6** in the Admin and chat apps (TypeScript 6 also in the repository
  scripts). Sign-in was verified against Keycloak 26.7. ESLint stays on 9 until `eslint-config-next`
  supports 10; Dependabot ignores it.
- **MCP SDK 2** in the backend and the tool worker. Tool discovery reads the SDK's renamed
  `input_schema`, tool-call results keep their `isError` and `structuredContent` keys, and the Hermes
  bridge endpoint now pins the MCP protocol revisions it implements instead of reading them from the SDK.

## v0.0.7 (2026-11-07)

Group membership editing in the Admin UI, operator runbooks, documentation publishing by CI and a pass over the dependencies.

### Added

- **Group membership can be edited in the Admin UI.** The Members tab of a group lists who
  is in it and where each membership comes from, and administrators can add people to any
  group by hand and remove them again. Sign-in synchronization with the Keycloak `groups`
  claim now touches only the memberships it created: people added by hand stay (the new
  `user_groups.source` column, migration `20261109_group_member_source`), and memberships
  from Keycloak remain managed in Keycloak. New endpoints:
  `GET/POST /v1/admin/groups/{id}/members` and `DELETE /v1/admin/groups/{id}/members/{userId}`.

### Changed

- **Dependencies moved to their current majors.** Node 24 LTS in the UI images and CI;
  redis-py 8, mypy 2, uvicorn 0.54, structlog 26, sse-starlette 3, aioboto3 15, pyarrow 26
  and pytest-cov 7 in the Python services; vitest 5, jsdom 30, jest-dom 7, mermaid 12,
  lucide-react 1 and tailwind-merge 3 (the release for Tailwind 4) in the frontends. The
  unused `zod` and `opensearch-py` dependencies are removed. pymilvus stays at 2.6.17: the
  mutation transport is audited against that exact SDK and refuses any other version.
  Dependabot is told to leave pymilvus, `node` and `@types/node` majors to us.

### Documentation

- **Operator runbooks.** Four new pages under `docs/operations/`: secrets and credentials
  (an inventory, which are applied only once, how each is rotated), backup and recovery (a
  dump and restore of all databases and roles, rehearsed on a second server), network
  enforcement (what is enforced where and how to verify it after a change) and changing
  models and providers. `CONFIGURATION.md` no longer names a shared Hermes sandbox network
  that does not exist: each sandbox has a `/28` of its own.
- **The documentation site is published by CI.** `scripts/build-docs-site.py` builds the pages of
  `docs/` (the same template as the published site, the current section marked in the navigation
  bar) and `.github/workflows/publish-docs.yml` publishes them to https://tsrman.github.io/aurumai/
  when a `vX.Y.Z` tag is pushed. It needs the repository secret `DOCS_SITE_TOKEN`; see
  [docs/RELEASE.md](docs/RELEASE.md#documentation-site).

### Upgrade notes

- One database migration since v0.0.6: `20261109_group_member_source` (adds `user_groups.source`, existing
  rows become `identity`). It runs with the backend entrypoint.
- Rebuild the images: the UI images moved to Node 24 and all Python services have new dependencies.
- Publishing the documentation site on a tag needs the repository secret `DOCS_SITE_TOKEN`.

## v0.0.6 (2026-11-07)

A fix for the chat app's sign-in page and a pass over the documentation's screenshots.

### Fixed

- The sign-in page showed the app's sidebar with the built-in demo agents, a dead "New chat" and a
  spinning history to a visitor who had not signed in. The sidebar now appears only for a signed-in
  user. (#180)

### Documentation

- The guides' screenshots are retaken on the current stack: the sign-in page, the comparison example
  (it had been taken while the answer was still streaming, so its table showed as raw text), the
  Hermes preview card and the running prototype. (#179, #180)
- The Hermes Agent guide's troubleshooting table names a cause of failing Hermes turns: the egress
  guard resolves the backend's address once, when it starts, so after the backend is recreated
  `hermes-egress-guard` and `hermes-egress` must be restarted. (#179)

### Upgrade notes

- No database migration since v0.0.5. Only business-ui changed; the other services are unchanged.

## v0.0.5 (2026-11-05)

Agents of the administrator become first-class in chat: chosen in the sidebar, created in a wizard, archived
instead of deleted, with Hermes agents that carry their own model, skills, knowledge bases and internet mode.

### User interface

- **Settings page in the design system.** The member settings page (`/settings`) and its Hermes
  memory and skills sections now use the shared cards, toggles and buttons instead of bare browser
  controls. The guide's screenshots were retaken on the current stack (real agents in the sidebar,
  the redesigned settings page).

### Agents

- **Hermes agents in workflows follow their settings.** A workflow node keeps its agent's own model
  and question switch (a question it may not ask is answered "skip"). The wizard can check that a
  chosen model answers with native tool calls (`POST /v1/admin/settings/hermes/model-check`).
- **Configurable Hermes agents.** A Hermes agent can use a model of its own, lower the internet
  mode of its workspace, take only selected skills, carry knowledge bases (searched by id with
  `knowledge_search`) and switch web search, images and questions off. The MCP bridge, the egress
  guard and the model route resolve the agent of each session live and fail closed when it is
  archived or gone. The wizard in the admin UI sets all of it; workflow templates carry the settings.
- **Archiving agents.** An agent that a workflow or a run ever referred to cannot be deleted, so it
  can now be archived: hidden from lists and pickers, refused for new use (`/execution`, workflows,
  runs), history intact, and restorable. `POST /v1/agent-definitions/{id}/archive` and `/unarchive`;
  `GET /v1/agent-definitions?includeArchived=true`. The admin UI has archive and restore actions and
  a **Show archived** switch.
- **Clear reasons instead of a bare 409.** Deleting or archiving an agent is refused with a coded
  reason (`agent_in_use`, `agent_has_history`, `agent_in_active_run`) naming the workflows involved, and
  the UI says why. A name held by an archived agent is reported as such.
- **Configurable agents and a creation wizard.** Each agent has three switches for its built-in
  capabilities in chat: web search and reading pages, image generation and editing, and questions
  to the user (`webEnabled`, `imagesEnabled`, `askUserEnabled`, all on by default, so existing
  agents keep working). They narrow what the platform's switches and the user's rights already
  allow, never widen it. The admin UI creates and edits agents in a step-by-step wizard (type,
  basics, instructions, tools and knowledge, capabilities, review).
- **Hermes Agents of the administrator in chat.** A Hermes-kind agent is chosen in chat like any
  other: its instructions follow the platform's own rules about the sandbox (files in
  `/workspace/inputs`, results in `/workspace/outputs`, attached with `attach_file`), and it runs on
  the organization's Hermes model. The conversation's workspace belongs to the first Hermes agent that
  ran in it. The platform's sandbox rules now name the outputs folder. Roll out business-backend,
  conversation-runner, tool-worker and skill-executor together; an older skill-executor refuses the
  agent's instructions field.
- **Agents in chat.** The sidebar and the agent picker list the agents an administrator created
  and granted, in place of the built-in demo agents, and a chat turn can run on one:
  `agentDefinitionId` in `POST /v1/chat/stream`, `GET /v1/chat/agent-definitions`. The turn takes
  the agent's model, instructions, skills and knowledge bases (web, image and question tools as the agent allows), the
  agent stays chosen for the conversation, and access, archiving and dependencies are checked on
  every turn. The conversation runner scopes the prompt and tools of an agent turn.

### Fixed

- A streaming answer no longer appears above its prompt when both messages have the same
  millisecond timestamp; the chat timeline preserves their insertion order.
- Deleting an agent that only past runs referred to ended in a 500 from the foreign key; it is now a
  409 that points to archiving.
- A failed Hermes sandbox Pod creation no longer deletes an existing Pod with the same name.
  Kubernetes cleanup checks the release, session, organization and conversation ownership and
  uses UID preconditions for graceful and forced deletion, preserving replacement Pods.

### Upgrade notes

- Three migrations extend `agent_definitions`: `20261106_agent_archive` (`archived_at`, `archived_by`),
  `20261107_agent_capabilities` (`web_enabled`, `images_enabled`, `ask_user_enabled`, default true) and
  `20261108_hermes_agent_settings` (`hermes_egress_mode`, `use_org_hermes_model`, `hermes_skills_scope`;
  it also drops the check that kept Hermes agents free of tools and knowledge bases). Existing agents
  keep working: every new column has a default that preserves today's behaviour.
- Roll out business-backend, conversation-runner, tool-worker and skill-executor together with
  business-ui and admin-ui: an agent turn signals the runner a new `agent` field, a Hermes agent's
  instructions need a skill-executor that accepts them, and the wizard needs the new admin API fields.
  An older runner ignores the agent, and an older skill-executor refuses the instructions field.
- A Hermes agent with its own model is offered only to users who may use that model.

## v0.0.4 (2026-11-01)

### Hermes Agent on Kubernetes

The Helm chart deploys Hermes Agent (`hermes.enabled`, default off); skill-executor runs the
sandboxes as Pods (`KubernetesHermesSandboxes`), sharing the lifecycle with the Docker runtime.

- **Boundary.** A restricted namespace, the `hermes-egress` guard (the proxy, without nftables:
  NetworkPolicy is the packet filter) and default-deny NetworkPolicies in which a sandbox reaches
  only the guard, with no DNS of its own. Egress modes `none`, `packages` and `public` bound by
  `hermes.egress.maxMode`. An internal package mirror is allowed by `cidr` or by namespace and Pod
  labels (`hermes.egress.internalDestinations`), never by a Service address.
- **Runtime.** One Pod per session; files, snapshots and the preview byte pipe go through
  `pods/exec`; an idle sandbox is snapshotted from its running Pod and removed, and a restart of
  skill-executor adopts live Pods by label. Capacity and workspace limits (a soft limit, and the
  kubelet's eviction as a backstop), and a RuntimeClass for a node PID limit.
- **Lost nodes.** The next `create` for a sandbox whose node was lost starts the session anew from
  its last snapshot, a Pod stuck Terminating is deleted by force, and `create` waits for a
  terminating Pod of the same name.
- **Previews.** `hermes.preview` deploys the preview service with a wildcard Ingress rule.
- **Acceptance.** `smoke-test-hermes-k8s.sh` (the boundary and the runtime on k3d, also on Calico with
  `HERMES_K8S_CNI=calico`), `smoke-test-hermes-k8s-egress.sh` (the egress modes against the real
  internet), `smoke-test-hermes-k8s-nodes.sh` (three nodes: a cold pull, eviction, a PID limit, a lost
  node) and `K8S_TEST_ACCEPTANCE=hermes ./smoke-test-k8s.sh` (Hermes on the whole platform over the
  Compose data plane: files in and out, the MCP bridge, memory, skill drafts, snapshot and restore,
  previews over TLS through the Ingress, and one turn on a real model).

### Fixed

- The chart rendered the image upload limit as `1.048576e+07`, which the backend could not parse, so a
  release with the default values did not start on Kubernetes.
- The preview service answered a WebSocket handshake without `Connection: Upgrade`, which browsers
  and proxies refuse, so WebSockets of a previewed app did not open.
- A flaky preview stream test in skill-executor.

### Documentation

The documentation gains an [index by audience](docs/README.md), user, administrator,
Hermes Agent, deployment and operations guides, an API reference and a security
model, and covers Hermes on Kubernetes. Stale Hermes and archive statements in the
existing documents are corrected. It is published at https://tsrman.github.io/aurumai/.

## Initial public source preparation

The first AurumAI source snapshot includes the existing chat/admin platform,
Temporal conversation runners, tools/sandboxes, RAG, durable workflows/questions,
web boundary and optional data lifecycle features.

Public preparation adds AGPL-3.0-only project licensing, attribution, AurumAI
branding, runtime source/license information, persistent development bootstrap,
generic provider configuration, English documentation/comments and source-release
validation. A new public Git history contains the reviewed source snapshot only.

Both user interfaces are localized in English and Russian with next-intl, with
browser-language negotiation and a persistent language switcher.

Rate limiting now counts chat streams and ordinary API requests as separate
per-user budgets, and the default ordinary limit is 300 requests per minute.
Keycloak token validation tolerates a configurable clock skew for issue and
not-before times (`APP_JWT_CLOCK_SKEW_SECONDS`, default 30) while expiry stays exact.
The admin grant editor no longer lets a background refetch discard unsaved edits,
and completed workflow progress cards survive the chat history refresh that
follows a finished turn.

Knowledge search's query-embedding timeout is configurable
(`RAG_API_SEARCH_EMBEDDING_TIMEOUT_SECONDS`, default 10). Workflow cancel/decision
and conversation-start failures that return 503 now log their cause.

The Admin UI is redesigned around the chat app's shell: a collapsible sidebar
with Users (users and groups), Agents, Skills and tools, Workflows, Knowledge
bases, Models and Analytics. Its layout and interactions follow Open WebUI's admin
panel: searchable tables with row actions, a group dialog with General,
Permissions and Members tabs, default permissions as their own entry, and access
configured from each resource's "Access" action instead of a separate page.
Agents can now be listed, edited and deleted, not only created. `/admin/access`
and `/agents/constructor` redirect to their new places. The admin API adds a
`memberCount` to groups and a `groupId` filter to `GET /v1/admin/users`.

Chat can create and edit images through an OpenAI-compatible Images API with the
built-in `generate_image` and `edit_image` tools. Organizations choose separate
generation and editing models in Admin UI (Models → Images); users need the new
`images.generate` capability, and the operator switch `IMAGES_ENABLED` is off by
default. Results are validated PNG/JPEG/WebP bytes stored per conversation and shown
under the answer. Editing works on images created earlier in the same conversation.

People can attach PNG, JPEG and WebP images to a chat message when the selected
model is one the organization marked in Admin UI (Models → Images) as accepting
images. The backend inlines them for the model at request time, keeps them in the
conversation history after a reload, and lists them as sources for `edit_image`.

Image generation and editing now count against the organization's monthly quota:
each Images API call is one request, plus the tokens the provider reports, and an
exhausted quota stops the tool with `image_quota_exceeded`. Usage events record the
kind of call, and Admin UI analytics shows images per model and per user.

Conversations with images can now be archived: the operator-only archive copies
each image's bytes into the archive bucket, verifies them, removes the platform copy
before purging the rows and puts the bytes back on restore. The data worker needs
separate platform-bucket credentials scoped to `conversation-images/*`
(`APP_ARCHIVE_IMAGES_S3_*`); without them such conversations stay live. The archive
format is now versioned (version 2 adds image counts and conversation images), which
also restores verification and restore of archives created before image support:
adding `interaction_turns.image_count` to the version-1 catalogue had made their
parts unreadable.

Workflows now work with images: node agents can create and edit images (a later
node can edit an earlier node's picture), the images appear under the workflow's
final answer, image calls are billed to the run and node, and attached images reach
the supervisor and every node for agents whose model accepts images.

A chat message sent while the conversation was just starting a child workflow (for
example a workflow run's orchestrator) could fail as "runner unavailable": the
execution check saw the child start before Temporal recorded it. The chat stream
now waits briefly for that start to settle and still blocks if it does not.

A workflow agent that called a built-in tool it was not offered, or passed
malformed arguments to one, no longer hangs: the failure used to be retried by
Temporal indefinitely. The agent now receives `unknown_tool` or
`invalid_arguments` as the tool result and can answer without it.

A workflow run cancelled from the chat no longer loses its `user_cancelled`
reason when the orchestrator later confirms the cancellation: repeating a run's
current status without a code keeps the recorded one.

`./smoke-test-archive-images.sh` adds an end-to-end acceptance for archiving
conversation images: on the disposable lifecycle project the real data-worker
archives and restores an old conversation with generated, edited and uploaded
images, with the image account limited by the documented MinIO policy, checked by
new independent `prepare-`/`check-data-lifecycle-images.py` helpers.

The first Hermes Agent phases (docs/HERMES_AGENT.md) add the backend side of a
future Hermes sandbox. `POST /v1/hermes/chat/completions` accepts OpenAI `tools`,
assistant `tool_calls`, `tool` messages and inline images, and streams the gateway's
chunks, tool call deltas included, unchanged, with the same capability, model grant,
vision and quota checks as the runner route. It accepts only a Hermes session token.
Sessions are records (`hermes_sessions`, one open per conversation) that a runner
opens, binds to the active turn with the turn's workflow run, phase and node, unbinds
and closes through `/v1/internal/hermes/sessions`. A token works only while its
session is open, unexpired (`APP_HERMES_SESSION_MAX_LIFETIME_SECONDS`, one day) and
on the same token version, so reopening or closing revokes earlier tokens, and calls
are billed to the bound workflow correlation. An open session keeps its conversation
out of the archive; the purge removes session rows. Opening and binding need the new
`agents.hermes` capability and the default-off `APP_HERMES_ENABLED` switch; nothing
calls these routes yet.

`docker-compose.hermes.yml` adds hermes-egress, the only way out of a future Hermes
sandbox: the completion path and a `CONNECT` proxy with three internet modes
(`none`, `packages` for the listed package indexes, `public` with `web.access`). The
operator caps the mode with `HERMES_EGRESS_MAX_MODE`, organizations choose within it
through `/v1/admin/settings/hermes`, and the proxy checks every tunnel with the
backend and closes open ones once the session, turn or mode no longer allows them.
The web-egress image gains a `hermes` guard mode; its web modes generate the same
rules as before. `./smoke-test-hermes-egress.sh` accepts it at runtime. The
`web.access` capability now also covers Hermes's public internet.

skill-executor can run long-lived Hermes Agent sandboxes on Docker from the new
`hermes-sandbox` image (Hermes 0.19.0 from PyPI, pinned by hash). Each sandbox has
no capabilities, a read-only root filesystem, a workspace volume and its own
internal network in isolated gateway mode, joined only by hermes-egress, so
sandboxes cannot reach each other or the host. The session token stays in the
workspace, never in container metadata; skill-executor talks to Hermes through
`docker exec`, re-adopts sandboxes after a restart and reaps idle or expired ones.
The Hermes completion route now also answers unstreamed requests with one JSON
completion, as Hermes's auxiliary calls need. `./smoke-test-hermes-sandbox.sh`
accepts it end to end.

A `publish images` workflow builds the service images on `v*` tags (or manual
dispatch) and pushes them to GitHub Container Registry under the chart's image
names.

Browser-facing Compose ports now bind to `PUBLIC_BIND_ADDRESS` (loopback by
default), the Keycloak `master` administrator password is generated by the
bootstrap script, and `docker-compose.proxy.yml` publishes the stack through
Traefik with Let's Encrypt TLS. Python dependencies with published advisories were
upgraded (PyJWT, pyarrow, pypdf, docling, urllib3, multidict, accelerate, pytest).

Evaluation now requires an explicit model for new child starts. A permanent
`evaluator-explicit-model-v1` Temporal marker preserves the legacy replay path.
Workflow patch markers, task queues and migration revisions are retained. Technical
identifiers use `aurum-ai`/`aurum_ai` (realm, database and roles, chart, labels,
buckets).

Public-source release/tag dates and verification results are recorded only after
the corresponding artifact has actually been built and approved. The private
repository's development/incident history is not imported into this changelog.
