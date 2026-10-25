# Changelog

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

The first Hermes Agent phase (docs/HERMES_AGENT.md) adds a native-tools completion
route, `POST /v1/hermes/chat/completions`, for a future Hermes sandbox. It accepts
OpenAI `tools`, assistant `tool_calls`, `tool` messages and inline images, and
streams the gateway's chunks, tool call deltas included, unchanged, with the same
capability, model grant, vision and quota checks as the runner route. It accepts
only a new Hermes session token, issued to a runner by
`POST /v1/internal/hermes/session-tokens` and usable only while its conversation has
an active turn. Both routes need the new `agents.hermes` capability and the
default-off `APP_HERMES_ENABLED` switch; nothing calls them yet.

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
