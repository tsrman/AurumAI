# Hermes Agent integration design

Status: approved design, not yet implemented (2026-10-25). Each phase below is a
separate flag-gated change; the open questions are settled in the PR that needs
them.

## Intent

The roadmap item reads "Integrate Hermes Agent as a universal system agent within
the existing conversation runtime, tools and workflow orchestration".
[Hermes Agent](https://github.com/NousResearch/hermes-agent) is Nous Research's
MIT-licensed agent runtime. It brings:

- a long-horizon tool loop with a terminal;
- curated memory (`MEMORY.md`, `USER.md`);
- skills it writes for itself;
- session search;
- subagents.

AurumAI should offer it as one more agent people can choose. It must not become a
second platform with its own identity, quotas or tenant model.

Chosen directions:

1. **Sandboxed process.** Each session runs one Hermes process in an ephemeral
   sandbox, in the same way code-skill sessions run. Every model call and every
   AurumAI tool call goes through platform endpoints. Grants, quotas, RLS and
   audit therefore apply unchanged.
2. **Selectable agent.** Hermes is a system agent that can be picked in chat and
   used as a workflow node. Ordinary chat does not change. Access requires a
   capability and an operator flag, and both are off by default.
3. **Per-user memory.** The platform stores each user's memory under RLS. Skills
   that Hermes writes become drafts, and an administrator approves them before
   anyone else can use them.

## Findings

What AurumAI provides today:

- **Model proxy.** The runner calls `POST /v1/chat/completions` on the business
  backend with an identity-only session token. Its request schema is
  `extra="forbid"`, and messages carry only `role`, string `content` and
  `image_ids`. The proxy has no `tools`, `tool_choice`, assistant `tool_calls` or
  `role: "tool"` messages, and it always streams upstream. AurumAI's own agents
  use the text protocol `<tool_call>{...}</tool_call>`.
- **Code-skill sandboxes.** `skill-executor` creates ephemeral sandboxes on Docker
  or Kubernetes. Each has a session TTL of 1800 s and an idle TTL of 300 s, with
  256 MB and 0.5 CPU. Networking is either `none` or `egress`. On Docker, `egress`
  is the NAT bridge `aurum-ai-skill-egress`; on Kubernetes it is a label-selected
  policy. No sandbox can reach the platform's internal services.
- **Durable sessions.** `CodeSkillSessionWorkflow` runs open, multi-round and close
  as bounded activities on the `tools` task queue. It uses `llm_call` with
  `publish_events=False`.
- **Narrow egress.** `web-egress` and `maintenance-egress` already show how to build
  an nftables allowlisted egress sidecar.

What Hermes provides (as of 2026-10):

- **API server.** It is enabled by `API_SERVER_ENABLED` and `API_SERVER_KEY` and
  started by `hermes gateway`. It listens on 127.0.0.1:8642 and offers:
  - `POST /v1/runs`;
  - `GET /v1/runs/{id}/events` as SSE, with `tool.started`, `tool.completed`,
    `message.interim`, `subagent.*`, `run.completed` and `run.interrupted`;
  - `POST /v1/runs/{id}/stop`, `/approval` and `/clarify`;
  - an `Idempotency-Key` header and a `max_concurrent_runs` limit.
  It also accepts inline images.
- **Main model.** It can be any OpenAI-compatible endpoint, configured with
  `model.provider: custom`, `model.base_url`, `model.default` and `model.key_env`.
  It relies on native function calling.
- **Configuration.**
  - `agent.disabled_toolsets` turns off whole toolsets.
  - `mcp_servers` accepts `url` and `headers` servers with `tools.include`
    filters and a `trust` setting.
  - `skills.write_approval` and `memory.write_approval` stage writes for review.
  - `memory_char_limit` defaults to 2200 and `user_char_limit` to 1375.
- **State.** Everything lives under `HERMES_HOME`: `config.yaml`, `.env`,
  `memories/`, `skills/`, `state.db` (SQLite with FTS5) and `pending/`.
- **Multi-user.** It is supported only through separate profiles. Within one
  profile, the API server gives the caller full toolset access, terminal included.

## Approaches

1. **Selected: a Hermes process inside a platform sandbox.** Hermes
   keeps its own loop, terminal and memory format. The platform owns identity, model
   access, tools, persistence and billing. The cost is a native-tools mode in the
   proxy, a narrow network path and a session bridge.
2. **A shared Hermes service, one profile per organization.** This is the fastest
   to build. But a shared process holding a terminal and long-lived state would sit
   outside RLS, live grant revocation and quotas, and one escape would expose every
   tenant. Not selected.
3. **Reimplement the Hermes features natively.** This gives full control, but it is
   a rewrite and gets none of Hermes's improvements. Not selected. It remains the
   fallback if Hermes's surface turns out to be unpinnable.

## Design

### Execution model

Hermes sessions can be long: one turn may run for hours, and a conversation
expects its terminal files, installed tools and Hermes state to still be there on
the next turn. The sandbox therefore belongs to the conversation, not to a turn,
and lives across turns. The code-skill TTLs (1800 s absolute, 300 s idle) do not
apply.

- **Workflow.** Each conversation that uses Hermes has one long-lived
  `HermesSessionWorkflow`, started on the first Hermes turn. `ConversationWorkflow`
  and a workflow node's `AgentWorkflow` hand turns to it with a Temporal Update and
  wait for the result. One run is active per session at a time: a second turn is
  rejected the same way the composer blocks a running conversation today. The
  session workflow uses continue-as-new to keep its history bounded. It is gated
  by `workflow.patched("hermes-v1")`, and replay tests cover histories from before
  and after the gate.
- **Activities.** All activities run on the `tools` queue and have bounded
  retries:
  1. `open_hermes_session` provisions the sandbox, or restores it from a snapshot,
     and hydrates `HERMES_HOME`.
  2. `run_hermes_turn` submits `POST /v1/runs` with an `Idempotency-Key` derived
     from the turn. It then reads `/events`, heartbeats every few seconds, and
     publishes safe progress. A retry after a lost worker reattaches to the same
     `run_id` through `GET /v1/runs/{id}` and `/events`: it does not start a second
     run. The Temporal timeout of the activity is the turn deadline; the heartbeat
     timeout (about 60 s) detects a dead worker.
  3. `snapshot_hermes_session` saves the workspace when the session goes idle.
  4. `close_hermes_session` reads back memory and skill drafts, takes a final
     snapshot and destroys the sandbox.
- **Durable sandbox ownership.** Today `skill-executor` keeps sessions in memory
  and reaps orphaned sandboxes after a restart. That would kill every long Hermes
  session on a deploy. Hermes sandboxes are recorded durably instead: labels on the
  container or Pod and a `hermes_sessions` row with org, user, conversation,
  sandbox id, state and timestamps. After a restart the executor re-adopts live
  Hermes sandboxes from that record rather than reaping them; it reaps only those
  past their limits or without a row.
- **Lifetime limits.** All of them are operator settings, enforced by the session
  workflow and the reaper:
  - turn deadline: default 2 h, maximum 24 h;
  - idle TTL between turns: default 30 min; at expiry the workspace is
    snapshotted and the sandbox stopped;
  - absolute session lifetime: default 24 h, then a snapshot and a fresh sandbox
    on the next turn;
  - snapshot retention: default 7 days after the last turn, and deleted with the
    conversation.
- **Snapshots.** A snapshot is a bounded archive (default 1 GiB) of the workspace
  and the session part of `HERMES_HOME`: `state.db`, working files and installed
  tools. Memory and skills are not in it; they follow the per-user flow below. It
  goes to the business bucket under `hermes-sessions/{org}/{conversation}/`, with a
  checksum recorded in `hermes_sessions`. Restoring verifies the checksum, unpacks
  only regular files and directories, and treats the content as untrusted. The
  conversation archive removes snapshots with the rest of the conversation;
  archives do not carry them.
- **Capacity.** A per-organization and an operator-wide cap on running Hermes
  sandboxes. Above the cap, the least recently used idle sandbox is snapshotted and
  stopped. If every sandbox is busy, the turn is rejected with
  `hermes_capacity_exhausted`.
- **Cancellation.** A cancelled turn calls `/stop`. The sandbox stays for the next
  turn.
- **Failure.** If the sandbox itself dies during a run, the turn ends as
  `hermes_interrupted`, and the next turn starts from the latest snapshot. A lost
  worker alone does not interrupt the turn, because the retry reattaches. Temporal
  records the platform's actions, not Hermes's internal state. The user and
  operator docs say so.
- **History.** The final answer is persisted through `persist_assistant_message`
  as usual. PostgreSQL stays authoritative: each run receives the complete
  persistent conversation history (`conversation_history` on `/v1/runs`). Hermes's
  `state.db` is only a working cache, rebuilt from that history when a snapshot is
  missing.
- **Browser independence.** A long turn runs on the server. Closing the browser
  does not stop it, and reopening the conversation shows the running turn and then
  its result, as with workflow runs today.

### Sandbox image and limits

- **Image.** A separate `hermes-sandbox` image installs Hermes with every
  dependency pinned by hash, with no build tools on the runtime path (packages
  without a wheel cannot be installed). It ships MIT attribution in
  `THIRD_PARTY_NOTICES`, and `AIR_GAPPED.md` lists it. `hermes update` cannot
  reach anything: the sandbox's only way out is hermes-egress.
- **Resources.** CPU, memory and PIDs get their own settings, well above the
  code-skill defaults: initially 2 CPU, 2 GiB of memory and 512 processes. The
  workspace volume for `HERMES_HOME` and working files has no size limit with
  Docker's local volume driver; workspace disk limits come with the capacity caps
  in phase 2c-ii. The lifetime limits are listed under the execution model.
- **Hermes configuration.** It is generated by the platform and placed in the
  workspace before the container starts; the session token and the local API
  server key are never in the container's environment or labels. The agent can
  overwrite the file, so no platform-side control depends on its contents.
  - `model`: the platform route (`http://hermes-egress:8080/v1/hermes`) with the
    session token as `api_key`. Hermes's auxiliary calls (titles, compression,
    vision) use the same endpoint and credentials without streaming, which the
    route answers with one JSON completion through the same checks and quota
    (seen in the spike and covered by route tests; Hermes does not make them on
    every run, so the sandbox acceptance checks only the main call). Hermes also
    tries to fetch model metadata from openrouter.ai; hermes-egress refuses it.
  - `terminal.backend: local`: the sandbox is the boundary, with no nested Docker.
  - `agent.disabled_toolsets`: browser, computer use, web and search, cron and
    kanban, messaging integrations, media generation and `clarify` (questions to
    the user arrive with phase 4).
  - `approvals.mode: off`: the sandbox is the boundary, and Hermes's hardline
    command patterns still apply.
  - The API server listens on 127.0.0.1 only; skill-executor reaches it through
    `docker exec` of `hermes-relay`, whose output it treats as untrusted.
  - Only HTTPS uses the egress proxy (`NO_PROXY` covers `hermes-egress`).

### Network path

This is the hardest boundary. The sandbox has no route of its own: everything
leaves through one platform component, `hermes-egress`, which follows the
`web-egress` pattern (nftables, a default-drop policy and readiness that proves the
rules are in place).

- On Docker, the sandbox runs on a dedicated internal network that has no NAT. The
  sidecar is the only platform member, but every sandbox shares the segment.
  Docker bridges let containers on one network reach each other, and a container
  holding `NET_RAW` (a Docker default) could spoof the sidecar's address and read
  another sandbox's session token on the wire. Phase 2c must therefore run
  sandboxes with every capability dropped and either give each sandbox its own
  internal network that the sidecar joins (sandbox CIDRs from a pool) or accept the
  shared segment as a Docker-only limitation that Kubernetes NetworkPolicies close.
  Its acceptance probes reachability from one sandbox to another.
- On Kubernetes, a NetworkPolicy keyed on the Hermes sandbox label allows egress
  only to the sidecar.
- The existing `aurum-ai-skill-egress` network is never reused, and the backend
  never joins a network that sandboxes join.
- The sandbox needs no DNS of its own: the platform endpoints and the egress proxy
  are addressed by the sidecar's name, and the proxy resolves internet hosts. That
  also closes DNS as an exfiltration channel.

`hermes-egress` has two roles.

1. **Platform endpoints.** A reverse proxy that forwards exactly two backend paths,
   the model proxy and the platform tools MCP endpoint, and nothing else.
2. **Internet egress.** A forward proxy (`HTTP_PROXY`/`HTTPS_PROXY` in the sandbox;
   `CONNECT` for HTTPS) used by `pip`/`uv`, `git`, `curl` and Hermes's terminal
   commands. Hermes works for hours and may need to install Python packages or
   fetch a file, so a closed sandbox is not enough. Three modes:

   | Mode | What the sandbox can reach |
   |---|---|
   | `none` | Nothing beyond the two platform endpoints. |
   | `packages` | Also the package index hosts on the operator's list, by default `pypi.org` and `files.pythonhosted.org`. |
   | `public` | Also any public host on ports 80 and 443. |

   - **Who chooses.** The operator sets the maximum
     (`APP_HERMES_EGRESS_MAX_MODE`, default `packages`) and the package host list.
     The organization administrator picks a mode within that maximum, by default
     `packages`. `public` also needs the user's live `web.access` capability.
   - **Per connection.** The sandbox authenticates to the proxy with its Hermes
     session token (`Proxy-Authorization`). For every new connection the proxy asks
     the backend whether the session is open, unexpired, on its token version and
     bound to an active turn, and which mode applies right now. The answer is cached
     for a few seconds at most. Closing or rotating the session, ending the turn,
     lowering the organization's mode or revoking `web.access` therefore stops new
     connections. The proxy holds no platform secret beyond its own service
     credential for that check.
   - **What stays closed.** Every mode refuses private, loopback, link-local and
     platform ranges plus `WEB_EGRESS_FORBIDDEN_CIDRS`. The proxy checks the
     resolved address before connecting, and the nftables guard enforces the same
     ranges underneath. A host on the operator's package list is the only exception,
     and only when the operator lists it explicitly, for example an internal package
     mirror in an air-gapped installation.
   - **Limits.** Connection and idle timeouts, concurrent connections per session
     and bytes per turn, all operator settings.
   - **Journal.** Each decision is recorded with the session, host, port, mode,
     outcome and byte counts. Paths and bodies of HTTPS traffic are never seen or
     recorded.

Search and reading pages stay platform tools: `web.search` and `web.read` through
the MCP bridge keep their SSRF policy, deadlines and citations whatever the mode.
Hermes's own web and browser toolsets stay disabled, since they would need provider
keys inside the sandbox. In `public` mode the terminal can still download a page or
clone a repository directly. Installed packages and downloaded files live in the
workspace and survive between turns through the snapshot.

The risk is stated in the admin UI next to the setting. In `packages` and `public`
modes, a prompt injection can send whatever the sandbox holds (its files and what
the conversation gave it) to a reachable host. In `packages` mode such a host is a
package index, which is a narrow but real channel through uploads to it. That is
why `public` needs `web.access` and an explicit organization choice.

Acceptance needs runtime evidence, not only static tests. From inside the sandbox,
in each mode:

- probes to MinIO, PostgreSQL, Temporal, skill-executor, other backend paths and
  private ranges must fail;
- the two platform calls must succeed;
- in `none`, every internet probe fails;
- in `packages`, `pip install` from the listed index works and other hosts fail;
- in `public`, a public host works and a private one still fails;
- a closed session or an ended turn stops new connections.

### Credentials in the sandbox

The model has a terminal. Anything in its environment or files can be read by the
model and by any prompt injection.

- **Session token.** The runner's session token never enters the sandbox. When a
  session opens, the backend mints a separate Hermes session token. It:
  - is bound to the `hermes_sessions` row: organization, user and conversation;
  - has the audience `hermes-session`;
  - is accepted only by the native-tools proxy route, the MCP bridge and the
    `hermes-egress` forward proxy, which checks it with the backend;
  - works only while a turn is active. The backend takes the current turn and,
    where relevant, the workflow run and node from the row, not from the token.
    Between turns every call fails with `hermes_turn_inactive`;
  - expires with the absolute session lifetime and is revoked on close or
    snapshot. A restored sandbox gets a new token.
- **What a leak can do.** Model and tool calls for that conversation, only while
  one of its turns is running, and still subject to live grants and quotas. A
  sandbox that sits idle for hours can do nothing with it.
- **Hermes API key.** `API_SERVER_KEY` is random per session and known only to the
  activity.
- **Provider credentials.** No provider credentials enter the sandbox.

### Native tools in the model proxy

A separate route, `POST /v1/hermes/chat/completions`, accepts the Hermes session
token only. It accepts:

- `tools`;
- `tool_choice`;
- assistant `tool_calls`;
- `role: "tool"` messages;
- multimodal user content.

It streams the gateway's `tool_calls` deltas back unchanged.

- **Checks.** It enforces the same capability, model grant, vision and quota checks
  as the existing route. `reserve` and `complete` apply per call, with
  workflow-run, phase and node correlation for nodes.
- **Langfuse.** Sanitization covers the new message shapes.
- **Existing route.** Its schema and the text tool protocol stay unchanged.

`LLM_GATEWAY_URL` must forward native tools. Today the docs only state that it is
OpenAI-compatible, so this becomes a documented gateway requirement. Startup does
not check it; the first Hermes turn fails clearly if the gateway rejects `tools`.

### Platform tools over MCP

A new MCP endpoint (HTTP/SSE) in the business backend, `/v1/hermes/mcp`, exposes
the AurumAI tools the user may use for this turn. Hermes's `mcp_servers` config
points at it with `tools.include`.

- **Tools exposed.** Knowledge search, `web.search` and `web.read`, image generation
  and editing, granted MCP skills and code skills, and model questions to the user.
- **Live authorization.** Every call re-authorizes. Revoking a grant during a turn
  takes effect on the next call.
- **Delegation.** Calls reuse the existing services: web through `web-tools`,
  images through `image_tools`, RAG through `rag-api`. MCP skills and code skills
  go through tool-worker activities, started by the bridge as child work of the
  session.
- **Idempotency.** Hermes does not provide Temporal invocation ids. The bridge
  derives an operation id from the session, the MCP request id and a hash of the
  arguments, and records it in `tool_operations`. A retried request returns the
  stored result instead of repeating the side effect. Images remain tied to the
  turn and execution as they are today.
- **Questions to the user.** These map to the existing durable `user_questions`
  flow. While a question is open, the run waits in Hermes's `/clarify` state.

### Memory and skills

- **Memory storage.** A new table `hermes_memories` holds `org_id`, `user_id`,
  `kind` (`memory` or `user`), `content`, `updated_at` and a `version`. It has
  FORCE RLS, and only the owning user can read or write it.
- **Memory in a session.** When a session opens, the user's memory is written into
  `HERMES_HOME/memories` within the configured character limits. When it closes,
  the files are read back. The content is treated as untrusted:
  - it is cut to the limits;
  - it is stored only if the version did not change in the meantime;
  - it is never shown to other users.
  The memory can be viewed and cleared in the user's settings, and it is deleted
  when the user is deleted. It is excluded from conversation archives, with a
  documented lifecycle position.
- **Skill drafts.** When a session closes, the platform diffs `HERMES_HOME/skills`
  against what it hydrated. New or changed skills become drafts in a new
  `hermes_skill_drafts` table owned by the user.
  - Hermes's `skills.write_approval` is not trusted as the gate, because the
    terminal can write the directory directly.
  - Approved drafts become ordinary platform skills: zipped `SKILL.md` and
    scripts, under the existing code-skill bundle validation.
  - Ordinary resource grants then control who can use them.
- **Hydrated skills.** The platform skills the user can access are hydrated
  read-only.
- **`state.db` and session search.** `state.db` lives in the conversation's
  snapshot, so session search covers that conversation. Search across the user's
  other conversations can later be offered as a platform tool scoped to the user's
  own conversations.

### Chat and workflow surface

- **Picking Hermes in chat.** Hermes is a system agent definition with type
  `hermes`. It is listed in the agent and workflow picker for users who have the
  new `agents.hermes` capability. The operator switch is `HERMES_ENABLED`, off by
  default. The organization chooses the Hermes model, which must be a model it has
  granted.
- **Public SSE.** It carries the answer text and safe tool progress: tool names,
  bounded status and sources. Reasoning, raw tool output, terminal output and
  memory content stay internal. Images appear under the answer as in ordinary chat.
- **Workflow node.** A node may use an agent definition of type `hermes`. The node
  brief and dependency outputs become the run input, and the node result is the
  final answer. Billing and correlation use the existing run, phase and node
  fields. Graph limits and concurrency are unchanged. A Hermes node counts as one
  node, and its subagents are bounded by Hermes limits set by the platform.

## Constraints

- No change to existing Temporal command histories without a `patched` gate, and
  no change to the text tool protocol, RLS, grant semantics or public-event privacy.
- Default-deny. There is no implicit capability for new or existing organizations,
  and no Hermes access without both the flag and the capability.
- Hermes is pinned and attributed. An upgrade is a deliberate PR with a re-run of
  the acceptance.
- English source and docs. Russian UI text goes only in `messages/ru.json`.

## Phasing

Each phase is a separate PR behind the flag:

1. **Native tools in the proxy.** The new route, a Hermes session token and quota
   correlation. Tested with a scripted gateway stream against a real database;
   acceptance through the deterministic gateway follows with the chat agent in
   phase 4, the first caller of the route.

   Implemented. Phase 2's session records (below) later replaced its short token
   expiry with revocation and its conversation-only billing with the bound turn's
   workflow correlation.

   Compose passes `APP_HERMES_ENABLED` with the `docker-compose.hermes.yml`
   overlay; the Helm chart does not yet. Like the runner route, the Hermes route always streams
   and ignores `stream: false`.
2. **Sandbox and network.** The `hermes-sandbox` image, its resources, durable
   sandbox ownership and re-adoption after an executor restart, snapshots, the
   `hermes-egress` sidecar on Docker and Kubernetes, and runtime egress probes.
   Split into three changes:
   - 2a, implemented: `hermes_sessions` rows with FORCE RLS, one open session per
     conversation, runner routes to open (rotating the token), bind a turn with its
     workflow correlation, unbind and close; tokens carry a token version, and an
     open unexpired session blocks archiving while the purge removes session rows.
     Sandbox id, snapshot and capacity columns arrive with 2c;
     Opening rotates the token, and a long-lived sandbox reads its token once at
     start: phase 4 opens a session only when it provisions or restores a sandbox,
     and every turn only binds and unbinds;
   - 2b, implemented for Docker Compose: `docker-compose.hermes.yml` with the
     guard (a `hermes` mode of the web-egress image) and the stdlib proxy, the
     backend's per-connection check and organization setting, accepted at runtime
     by `./smoke-test-hermes-egress.sh`. The forward proxy accepts `CONNECT` only;
     its journal is structured log lines. Docker's embedded DNS answers no external
     names on the internal sandbox network (checked by the acceptance). Still
     open: Kubernetes (Helm, NetworkPolicies, and no kube-dns for sandbox pods so
     CoreDNS cannot become a DNS channel) and the Admin UI control (phase 4);
   - 2c-i, implemented for Docker: the `hermes-sandbox` image and long-lived
     sandboxes in `skill-executor` (create, re-adopt, run, events, stop, close,
     idle and absolute lifetimes, guard reconnection), each on its own internal
     network in isolated gateway mode, accepted at runtime by
     `./smoke-test-hermes-sandbox.sh`. Deviations from the text above:
     - Hermes comes from PyPI (`hermes-agent==0.19.0`) with every dependency
       pinned by hash, not from a pinned Git commit: hashes, package mirrors and
       air-gapped installs work the same way as for other images. That release
       is older than the newest Git tag; upgrading is a requirements recompile
       plus the acceptance;
     - Hermes 0.19 does not replay `Idempotency-Key`, so `hermes-relay` records
       each run id in the workspace and a retried run attaches to it;
     - ownership is in Docker labels only; linking a sandbox to its
       `hermes_sessions` row is phase 4's, because skill-executor has no database;
     - the Kubernetes runtime refuses Hermes sandboxes explicitly;
     - known window: the relay records a run id only after Hermes accepts the
       run, so a relay that dies in between can make a retry start a second run;
     - attaching to a run replays its events from the start, so phase 4
       deduplicates them;
     - skill-executor's Hermes routes trust any caller that knows a session id,
       like its code-skill routes; phase 4 keeps it reachable only from the tools
       worker;
   - 2c-ii: workspace snapshots, capacity caps and workspace disk limits.
3. **MCP bridge.** Knowledge, web, images and questions. MCP skills and code skills
   come later in the same phase if the scope allows.
4. **Chat agent.** The long-lived `HermesSessionWorkflow` with turn hand-off,
   reattach after a lost worker, idle snapshot, capacity caps, events, cancel, the
   picker, the capability and flag, the UI and acceptance.
5. **Memory and skill drafts.** Tables, hydrate and read-back, user settings and
   admin approval.
6. **Workflow node.** A `hermes` node type, billing and replay tests, and the
   choice for parallel Hermes nodes (see the open questions).

Recommended v1 for real use: phases 1–4. Memory, drafts and nodes follow.

## Validation

- **Code.** TDD per phase. Replay tests for the `hermes-v1` gate. RLS tests for the
  new tables. Proxy tests for native tool shapes and quota correlation.
- **Network.** Runtime egress probes on Docker and in a Kubernetes test cluster.
- **Isolation.** Credentials are proven absent from the sandbox: no runner token
  and no provider key in its environment or files.
- **Acceptance.** Deterministic acceptance with the test gateway scripting native
  tool calls. Then a few paid real-provider checks: a chat turn with web and an
  image, and a workflow node.
- **Long sessions.** Deterministic tests for a turn that outlives several
  heartbeats, a worker killed mid-run that reattaches without a second run, an
  executor restart that re-adopts the sandbox, idle snapshot and restore with the
  workspace intact, an expired token between turns, and capacity eviction.
- **Reporting.** Which runtime combinations were actually exercised is reported
  separately.

## Open questions for review

- Whether code skills should be reachable through the bridge in v1, given that
  Hermes already has a terminal in its own sandbox.
- Default resource limits, lifetime limits and the capacity caps.
- Egress defaults: bytes per turn and concurrent connections, and whether the
  default package list should also cover npm or Git hosts.
- Parallel Hermes workflow nodes. A conversation has one open session with one
  turn binding, so two Hermes nodes running at once in the same run would bill
  and revoke through the same binding. Phase 6 either runs a conversation's Hermes
  nodes one at a time or gives node sessions their own scope (a cheap change to
  the one-open-session index).
