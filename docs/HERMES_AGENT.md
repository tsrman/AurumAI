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
  Docker's local volume driver; phase 2c-ii-b adds a soft limit from Docker's
  volume usage or, by operator choice, a size-bounded tmpfs workspace. The
  lifetime limits are listed under the execution model.
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
  - `mcp_servers.aurum`: the platform tools at
    `http://hermes-egress:8080/v1/hermes/mcp` with the session token, the bridge's
    tools only, no preflight probe and no sampling.
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
   | `packages` | Also the package index hosts on the operator's list, by default `pypi.org`, `files.pythonhosted.org` and `registry.npmjs.org`. |
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
- **Upstream failures.** The sandbox gets only `upstream_error <status> <type>`,
  where the type is the provider's error type when it is a short token
  (`upstream_error 402 free_request_quota_exhausted`). The provider's message and
  the gateway's addresses stay in the backend's log. Hermes puts this text in
  the failed run's output, and tool-worker turns it into a fixed code, never
  forwarding the text: `hermes_model_unavailable` for 402 and 429 (quota, rate
  limit, payment), `hermes_model_error` for other statuses, and
  `hermes_run_failed` otherwise. The runner ends the failed turn with a reason
  the user can act on, followed by the code, and closes the turn with it: the
  reason is kept as the turn's answer, marked failed, so the history shows it
  and the runner never sends it back to a model (this holds for every failed
  chat turn).

`LLM_GATEWAY_URL` must forward native tools. Today the docs only state that it is
OpenAI-compatible, so this becomes a documented gateway requirement. Startup does
not check it; the first Hermes turn fails clearly if the gateway rejects `tools`.

### Platform tools over MCP

A new MCP endpoint (HTTP/SSE) in the business backend, `/v1/hermes/mcp`, exposes
the AurumAI tools the user may use for this turn. Hermes's `mcp_servers` config
points at it with `tools.include`.

- **Tools exposed.** Knowledge search, `web.search` and `web.read`, image generation
  and editing, granted MCP skills and code skills, model questions to the user, and
  `attach_file` for the files and charts it makes (see Files and charts).
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
- **Skill drafts.** After every turn (phase 5b; the design said when a session
  closes), the platform diffs `HERMES_HOME/skills` against what it hydrated. New or changed skills become drafts in a new
  `hermes_skill_drafts` table owned by the user.
  - Hermes's `skills.write_approval` is not trusted as the gate, because the
    terminal can write the directory directly.
  - Approved drafts become ordinary platform skills: zipped `SKILL.md` and
    scripts, under the existing code-skill bundle validation.
  - Ordinary resource grants then control who can use them.
- **Hydrated skills.** The platform skills the user can access are hydrated
  read-only (phase 5c: code skills, before every turn).
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
     - Hermes first came from PyPI (`hermes-agent==0.19.0`, the newest release
       published there); see "Hermes 0.22" below for how it is installed now;
     - Hermes 0.19 did not replay `Idempotency-Key`, so `hermes-relay` records
       each run id in the workspace and a retried run attaches to it (kept with
       0.22, whose idempotency store would also do);
     - ownership is in Docker labels only; linking a sandbox to its
       `hermes_sessions` row is phase 4's, because skill-executor has no database;
     - the Kubernetes runtime refuses Hermes sandboxes explicitly;
     - known window: the relay records a run id only after Hermes accepts the
       run, so a relay that dies in between can make a retry start a second run;
     - attaching to a run was expected to replay its events from the start.
       Hermes 0.19 handed them to one subscriber only, once; 0.22 replays them
       (see "Hermes 0.22"), and the relay still falls back to polling the run's
       status when they are gone;
     - skill-executor's Hermes routes trust any caller that knows a session id,
       like its code-skill routes; phase 4 keeps it reachable only from the tools
       worker;
   - 2c-ii, split into two changes:
     - 2c-ii-a, implemented for Docker: workspace snapshots. A sandbox past its
       idle or absolute lifetime is stopped, snapshotted and removed, and the
       session's next create restores the snapshot into a new sandbox, under
       fresh secrets. `POST /hermes/sandboxes/{id}/snapshot` takes one in place
       and `DELETE ...?snapshot=true` takes the final one; both return
       `{sha256, size, created_at}`. Accepted at runtime by
       `./smoke-test-hermes-sandbox.sh` with a disposable MinIO. Deviations:
       - skill-executor writes the snapshot itself, stored by its SHA-256 next
         to a `latest.json` pointer, and a restore is checked against the
         object's name. Until phase 4 records the checksum in the session row
         (and passes it as `snapshot_sha256`), whoever can write the prefix can
         replace a snapshot, so operators scope skill-executor's credentials to
         `hermes-sessions/*`;
       - an idle sandbox is removed once its snapshot is stored. If the
         snapshot fails, it stays stopped with its volume, is retried, can be
         resumed by the next create with a new token, and is removed after the
         snapshot retention;
       - snapshots keep regular files and directories only (links, including
         those of a virtualenv, are dropped) and never contain the generated
         config, `.aurum/`, logs, memory or skills;
       - a snapshot in place reads a running sandbox, so `state.db` may be
         mid-write; it is only a cache, rebuilt from the conversation history;
       - retention is enforced by skill-executor (objects older than seven
         days); deleting a conversation's snapshots in the archive purge comes
         with phase 4, so until then they outlive a purge by up to the
         retention;
     - 2c-ii-b, implemented for Docker: capacity caps with least-recently-used
       eviction and workspace disk limits, accepted by the same acceptance.
       Deviations:
       - "busy" is an open stream from skill-executor or a run the relay
         (`hermes-relay active`) reports as queued, running or waiting; that is
         the sandbox's own word, so a sandbox claiming to be busy is still
         retired at its idle or absolute lifetime;
       - the disk limit is soft by default (Docker's volume usage, checked
         periodically; over it, active runs are stopped and new ones refused),
         or a size-bounded tmpfs workspace by operator choice. A tmpfs
         sandbox is paused instead of stopped when its snapshot fails and is
         never resumed, since its Hermes would keep the old token;
       - a workspace without the platform's own files (a tmpfs workspace after
         a restart), or one over the snapshot bound, is removed without a
         snapshot, so the last snapshot that fit stays the latest;
       - eviction snapshots the victim while the capacity lock is held, so
         creations wait for it.
3. **MCP bridge.** Split into two changes:
   - 3a, implemented: `POST /v1/hermes/mcp` with knowledge search, `web.search`,
     `web.read`, image generation and editing; hermes-egress forwards it, and the
     generated Hermes config connects to it. Accepted at runtime by
     `./smoke-test-hermes-sandbox.sh`: Hermes generates an image through
     hermes-egress as a completed operation of the bound execution, and between
     turns the sandbox lists the tools but cannot call them. Deviations:
     - binding a turn also names the registered execution acting in it (a new
       `hermes_sessions.active_execution_id`, kept inside the bound turn by a
       foreign key; its phase and node must match the binding's). Web and image
       calls are durable operations of that execution and reuse the existing
       services unchanged: live capabilities, per-turn budgets, quota, sources
       and images. The invocation id is derived from the session, the JSON-RPC
       id, the tool and a hash of the arguments, so a reused id with the same
       arguments returns the stored result;
     - Hermes connects to the bridge and lists its tools once, when the sandbox
       starts, which is usually between turns. The handshake, `ping` and
       `tools/list` therefore need only an open session on the token's version,
       not an active turn, and the catalogue is fixed (it follows only the
       operator switches); every call is authorized live. This narrows "between
       turns every call fails" to calls that read or do something;
     - the bridge is Streamable HTTP without a session, written as a small
       JSON-RPC route rather than the MCP SDK's server: one JSON answer per POST,
       no `Mcp-Session-Id`, so the client never opens the server stream that
       hermes-egress would refuse. Calls the bridge denies or cannot complete
       are tool errors with bounded codes, not HTTP errors, which Hermes counts
       towards pausing the server; the ordinary rate limit still answers 429;
     - knowledge search covers only the files attached to the conversation, as
       for an ordinary chat agent; the Hermes agent definition's knowledge bases
       follow with phase 4;
     - questions to the user move to phase 4: they wait in Hermes's `clarify`
       state, answered through the durable `user_questions` flow by the session
       workflow, not over a tool call bounded by its timeout;
   - 3b, implemented: platform skills. Two fixed tools, `list_skills` and
     `run_skill`, since Hermes lists the bridge's tools only once. Accepted at
     runtime by `./smoke-test-hermes-sandbox.sh`: Hermes runs a granted MCP skill
     through tool-worker against the reference MCP server. Deviations:
     - only MCP and tool skills qualify, and only those the user holds a grant
       for (a Hermes token always acts as a member); a conversation's own
       attachment search is `knowledge_search`. Code skills reach Hermes with
       phase 5, hydrated into its skills and run in its own terminal, which
       settles that open question; agent skills are not offered;
     - the listing holds ids, names, descriptions and input schemas only, never
       endpoints or credentials;
     - a run is a `HermesSkillCallWorkflow` on tool-worker's queue that runs
       `execute_skill`, as the runner's calls do; tool-worker remains the only
       service that reaches skill endpoints. Its workflow id, derived like the
       3a invocation ids, is the record instead of `tool_operations`: a
       repeated request attaches to the same run, until Temporal's retention
       ends it. A failed skill is a bare `skill_failed`; the bridge waits
       `APP_HERMES_SKILL_TIMEOUT_SECONDS` (240) and then answers
       `skill_timeout` while the run continues, so a retry attaches to it;
     - tool-worker fetches the skill with a runner session token for the
       conversation, which the bridge mints for each run with the Hermes
       token's generation. That token is stronger than the Hermes token (thirty
       minutes, whether a turn is active or not, and the runner's internal
       routes); it never reaches the sandbox, and it is in the workflow's
       history as it already is in the runner's skill calls;
     - skill runs have no per-turn budget; the ordinary rate limit bounds them.
4. **Chat agent.** Split into three changes:
   - 4a, implemented: the runtime and the API. A chat request with
     `agent: "hermes"` runs on the organization's Hermes model (set through
     `PUT /v1/admin/settings/hermes`), needs the switch and both capabilities,
     and is signalled to the runner as a Hermes turn. `ConversationWorkflow`
     (behind `workflow.patched("hermes-v1")`, evaluated only for Hermes turns)
     registers the turn's ordinary execution, hands the turn to the
     conversation's `HermesSessionWorkflow` on tool-worker's queue, waits for
     its result and persists the answer. Accepted at runtime by
     `./smoke-test-hermes-sandbox.sh` with a conversation runner in direct mode
     and the turn signalled through Temporal; the chat route itself is covered
     by route tests. Deviations:
     - Python workflow code cannot send another workflow an Update, so the turn
       is handed over with signal-with-start (from a runner activity) and
       answered with a `hermes_turn_result` signal; a cancelled turn is
       signalled as well;
     - the session workflow keeps the sandbox only as long as turns arrive:
       after the idle time (`hermes_idle_seconds`, 30 minutes) it snapshots and
       removes the sandbox and closes the session, revoking its token, and ends;
       the next turn starts a new run. It continues-as-new after 50 turns.
       Capacity, the absolute lifetime and re-adoption stay with skill-executor;
     - a running sandbox keeps the token it read at its start, so the session
       workflow reuses an open session whose sandbox runs (a new runner route
       returns it without rotating the token), and otherwise opens one, which
       rotates the token, and creates, restores or resumes the sandbox with it.
       The sandbox id is the session row's id, which links the two;
     - every backend call of the session workflow takes a fresh runner token
       from the waiting runner (`get_session_token` on its task queue, bounded
       at two minutes so a runner that is gone fails the turn instead of
       hanging the session), since a runner token lives thirty minutes and a
       turn for hours. The runner keeps itself and its token alive while it
       waits: every five minutes it calls `POST
       /v1/internal/hermes/turns/{turn}/touch`, which does what a chat request
       does for other turns. An idle close whose runner has gone leaves the
       session to expire;
     - the run activity heartbeats on a timer, and the turn deadline covers
       every attempt; a lost worker's retry attaches to the same run by its
       key. A sandbox retired between preparing and running is prepared again
       once. A cancelled turn reaches its run within seconds (tool-worker's
       heartbeat throttle is five seconds), and a turn the session never
       answers is cancelled after its deadline. The session remembers its last
       hundred turns, so a repeated hand-off never runs one twice;
     - the public stream carries the answer at the end, with the progress of
       4b. Images in a Hermes turn are refused for now;
   - 4b, implemented: the composer offers Hermes Agent (from `GET
     /v1/chat/agents`, only when a turn would be accepted) and keeps it chosen for
     the conversation; the Models page of the Admin UI sets the Hermes model and
     the egress mode (modes above the operator's maximum are disabled); the run's
     progress reaches the chat stream; English and Russian strings. Accepted at
     runtime by `./smoke-test-hermes-sandbox.sh`: a chat turn's tool card, its
     result and keepalives reach the conversation's channel, a tool argument
     never does, and the answer is persisted with the card. Deviations:
     - tool-worker publishes the progress to the conversation's event channel
       itself (it already had the Redis URL), as tool cards: `tool.started` and
       `tool.completed` become `tool-calls` and `tool-result`, paired by tool
       name in order since Hermes's events carry no call id. Only an allowlisted
       kind leaves the worker (`hermes.terminal`, `hermes.files`,
       `hermes.knowledge`, ..., else `hermes.other`), never the tool's name as
       Hermes sent it, its arguments, previews, reasoning or output; at most 50
       cards a turn. The answer persists with the cards of the attempt that
       finished, so a reload shows them;
     - no live answer deltas: the answer arrives with `message-end`. A delta
       stream could not be deduplicated after a reattach;
     - keepalives every 30 s, from preparing the sandbox (a restore takes
       minutes) to the run's end, hold the chat stream open past
       `EVENT_SUBSCRIBE_TIMEOUT`; the stream drops them. The turn routes'
       stream projects durable status only and is unchanged;
     - Hermes 0.19 handed a run's events to one subscriber, once: a retried run
       activity saw the events from its reattach on, under call ids that
       carried the attempt, and the relay, finding no events (404) or only
       keepalives of a queue another subscriber holds, polls the run's status
       for the result (covered by the relay's tests, not exercised at runtime).
       With 0.22 a reattach replays the run's events and cards take their ids
       from Hermes's tool call ids, hashed, so the replay updates the cards the
       lost attempt showed and the answer persists all of them;
     - a reopened conversation starts in the ordinary chat; Hermes is chosen
       again for the next turn;
     - sources and images under the answer, which ordinary chat shows, come
       later: the bridge's results are not read back into the answer yet;
   - 4c, split into two changes:
     - 4c-i, implemented: questions to the user through Hermes's `clarify`
       (Hermes 0.22). Accepted at runtime by `./smoke-test-hermes-sandbox.sh`:
       a question becomes the turn's question card, the user's answer through
       the ordinary answer route reaches Hermes, and with questions off the
       question is skipped. How it works:
       - skill-executor enables `clarify` for the API server
         (`platform_toolsets.api_server`, which leaves it out by default) and
         sets Hermes's own wait (`agent.clarify_timeout`) past the platform
         question's hour; answers go through `hermes-relay clarify` from a
         file and `POST /hermes/sandboxes/{id}/runs/{run}/clarify`;
       - the run activity forwards each `clarify.request` and
         `clarify.settled` to the conversation workflow, bounded: an id of a
         fixed shape, the question without Hermes's reply hint (4000
         characters), at most eight choices without its "(Recommended)" mark;
       - the conversation workflow asks it as an ordinary question of the turn
         (one at a time, as Hermes asks them) inside its five-minute touches,
         and signals the answer to the session workflow, which answers the run
         while it follows it. A chosen option goes back as the choice's text;
       - a question the user cannot be asked (questions off, the turn's limit)
         or does not answer before the card expires is answered `skip`; a
         question Hermes settles itself (its timeout, the run ended) loses its
         card. Several answers to a multi-select question are typed as text;
       - a `clarify` card shows "Asking you" while Hermes waits.
     - 4c-ii, implemented: the archive purge removes a conversation's Hermes
       sandboxes and workspace snapshots. The backend's object store
       credentials are scoped to `conversation-images/`, and only tool-worker
       reaches skill-executor, so the maintenance worker's purge starts
       tool-worker's `HermesConversationPurgeWorkflow` (one id per
       conversation; a resumed purge attaches or repeats it) and waits for it.
       It runs after the image purge and before the rows go, and only for a
       conversation with `hermes_sessions` rows, which the row purge removes,
       so a crash in between repeats it; a purge that cannot reach it, or a
       worker without it, leaves the conversation in place. skill-executor's
       `DELETE /hermes/conversations/{org}/{conversation}` closes the
       conversation's sandboxes without a snapshot (waiting for one in
       progress) and then deletes every object under its
       `hermes-sessions/{org}/{conversation}/` prefix. Workspaces are runtime
       state, not history: they are not archived, and a restored conversation
       starts Hermes in a fresh one. Accepted at runtime by
       `./smoke-test-hermes-sandbox.sh` through the workflow itself; the
       archive saga's step is covered by the backend's archive tests. A
       versioned bucket keeps the deleted objects' older versions; that is
       the operator's retention to set. The snapshot checksum is not recorded
       in the session row: skill-executor retires sandboxes on its own and
       the backend never sees it, and the purge needs only the prefix.
   - Hermes 0.22, implemented: the sandbox runs Hermes 0.22.5. Hermes no longer
     ships as a package (its build refuses outside Nix, and PyPI ends at 0.19.0),
     so the image installs the release's source tree from its GitHub tag
     archive, verified by its sha256, on Python 3.14, which 0.22 requires; its
     dependencies are exported from Hermes's own `uv.lock` (the `mcp` and `sms`
     extras, the latter for aiohttp) and installed by hash, as wheels only
     (`hermes-sandbox/update-requirements.sh`). Changes the upgrade brought:
     - MCP tools sit behind a `tool_search` bridge by default, which would
       hide the platform tools' names from the model; the config turns it off;
     - a `connections` toolset reaches Nous's connector gateway; it is
       disabled, with connectors and update checks off in the config;
     - the API server refuses a key shorter than 16 characters (skill-executor
       generates 43) and warns when it is PID 1, so sandboxes run with an init;
     - run events are numbered and replayed to every subscriber, tool events
       carry `tool_call_id`, and `tool.completed` carries a redacted preview of
       the tool's output, which the worker never forwards;
     - Hermes copies `config.yaml`, the session token and API key included, to
       `.hermes/backups/config/` whenever it parses; snapshots exclude
       `.hermes/backups` as they exclude the config;
     - the gateway opens a loop-liveness ping on a random 127.0.0.1 port (its
       Unix socket path would be too long); it carries no control, and the
       acceptance still checks that nothing listens beyond loopback;
     - Debian's SQLite (3.46) predates Hermes's WAL fix, so Hermes uses its
       rollback journal instead.
5. **Memory and skill drafts.** Split into three changes:
   - 5a, implemented: memory. `hermes_memories` (organization, user, `memory` or
     `user`, content within Hermes's 2200 and 1375 characters, version) has
     FORCE RLS by organization, and every query also names the owning user; it
     cascades with the user (the platform has no user deletion path yet, as for
     `user_settings`). It is not conversation data: conversation archives
     neither carry nor remove it. Accepted at runtime by
     `./smoke-test-hermes-sandbox.sh`: what Hermes's memory tool writes in a turn
     is stored for the user, and a fresh sandbox, which snapshots never carry
     memory into, starts with it. How it works:
     - while the turn is bound, the session workflow writes the user's memory
       into the sandbox before the run (`hermes_load_memory`) and reads it back
       after it, before unbinding (`hermes_save_memory`); the activities carry
       only versions and hashes, so the memory stays out of Temporal history.
       The steps are gated by `workflow.patched("hermes-memory")`, and a
       failed step never fails the turn;
     - what comes back is the sandbox's: `hermes-relay memory-read` takes only
       regular files (no links or FIFOs), bounded and valid UTF-8, and the
       backend cuts it to the limits at an entry boundary;
     - it is stored only on the version it was written at
       (`PUT /v1/internal/hermes/memory` with `baseVersion`; a missing row is
       version 0), so a turn in another conversation, or the user clearing
       it, wins over an older read. Clearing keeps the rows, empty, on a new
       version;
     - Hermes freezes the memory into a session's prompt at the session's
       first run: a sandbox that keeps running sees changes made elsewhere in
       its files (its own writes merge with them) but in its prompt only after
       it starts again, at the latest after its idle time;
     - users see and clear their own memory in their settings
       (`GET`/`DELETE /v1/hermes/memory`), shown where Hermes can be used.
   - 5b, implemented: skill drafts and their approval by an administrator.
     `hermes_skill_drafts` (organization, author, the skill's directory, name
     and description from `SKILL.md`, a hash of the file set, the bundle, a
     status of `pending`, `approved`, `rejected` or `dismissed`, and the skill
     an approval made) has FORCE RLS by organization; the user routes also
     name the author, and rows cascade with the user. Drafts are user data, not
     conversation data: conversation archives neither carry nor remove them.
     Accepted at runtime by `./smoke-test-hermes-sandbox.sh`: none of the turns
     before drafts bundled skills, a skill Hermes writes with `skill_manage` is
     drafted once for the user without the sandbox's secrets, and neither the
     unchanged skill nor the draft the user withdrew comes back on later turns.
     The approval itself is covered by the backend's tests, since the
     acceptance stack has no admin backend. How it works, and where it departs
     from the design above:
     - the skills are read after every turn, while the turn is still bound,
       not when the session closes: `.hermes/skills` is not in snapshots and
       skill-executor retires sandboxes on its own, so a read at close would
       miss skills. The session workflow's `hermes_save_skills` step follows
       the memory read-back, is gated by
       `workflow.patched("hermes-skill-drafts")`, carries only counts, and
       never fails the turn;
     - the baseline is the image's read-only `/opt/hermes/skills`, not
       Hermes's `.bundled_manifest`, which the agent can rewrite: a skill
       directory whose files equal the bundled copy's is skipped, a changed or
       new one is read. Hermes's own state at the root of `skills/` (dot
       files, `.hub`, `.archive`, `.locks`) and caches are never read;
     - `hermes-relay skills-read` reads the same way as `memory-read`: a
       `skills` directory that is a link is no skills, links are never
       followed, and only regular, valid UTF-8 text files count. A skill
       with anything else (a link, a binary, more than 32 files, a file over
       64 KiB or more than 96 KiB in all) is skipped whole, and at most 16
       skills are read per turn, one line each, the most recently changed
       first, so skills read on earlier turns do not crowd out a new one;
     - the backend checks the paths and bounds again, builds the bundle
       itself (stored zip entries, fixed timestamps, so the same files give
       the same checksum) and validates it as an uploaded code skill. A
       draft is keyed by its file set's hash per user across every status,
       so a skill seen again after a rejection or a withdrawal is not
       drafted again; a changed skill replaces its directory's open draft,
       and a user has at most 50 open drafts. Hermes's own
       `skills.write_approval` stays off: the terminal writes the directory
       anyway;
     - administrators review on the Admin UI's Skills page, which shows every
       file. A decision names the bundle checksum of the version on screen
       and is refused (`hermes_skill_draft_changed`) if a later turn replaced
       the draft's files meanwhile. Approval checks the stored bundle against
       its checksum and
       makes a code skill owned by the reviewer, with read access for the
       author, both grants audited as the reviewer's; others get access
       through ordinary grants. Approving a later
       version of an approved skill directory replaces that skill's bundle
       instead of adding a skill. Users see their drafts in their settings
       and can withdraw an open one (`GET`/`DELETE /v1/hermes/skill-drafts`);
     - bundles are stored like uploaded ones, by checksum under
       `skills/{org}/`, and like a replaced bundle, a declined or withdrawn
       draft's object is not removed.
   - 5c, implemented: platform code skills in the sandbox, read-only. Accepted
     at runtime by `./smoke-test-hermes-sandbox.sh`: a code skill granted to
     the user is in the running sandbox before the next turn with read-only
     files the sandbox cannot write, Hermes's `skills_list` lists it, it is no
     draft, and once the grant is revoked the next turn's sandbox no longer
     has it and Hermes no longer lists it. How it works:
     - after binding and before the run, the session workflow's
       `hermes_hydrate_skills` step (gated by
       `workflow.patched("hermes-platform-skills")`) lists the conversation
       owner's code skills (`GET /v1/internal/hermes/platform-skills`: the
       code skills the user may read, without per-conversation ones, at most
       32), and skill-executor fetches each bundle, checks it against its
       checksum, and unpacks it into a staging directory, which `hermes-relay
       skills-hydrate` swaps into `/workspace/.aurum/platform-skills`, one
       directory per skill (`{name}-{id prefix}`);
     - it fails closed. A skill that does not verify or fit the bounds (512
       entries, 8 MiB each, 32 MiB in all, well below the workspace limit;
       bundles are fetched again for every turn) is left out, staging that
       a failed hydration left behind is removed by the next one; when the list cannot be had or written, the directory is
       emptied instead; and when even that fails, the turn does not run, so
       a revoked skill is not offered from the next turn on (a turn already
       running, which a question can keep open for over an hour, keeps what
       it started with). Revoking cannot take back what the
       agent already copied elsewhere in its workspace, which snapshots keep;
     - the directory is one of Hermes's `skills.external_dirs`. Hermes keeps
       the external directories it found at its start, by their real path,
       so skill-executor creates the directory with the config and only its
       entries change; a sandbox adopted from before this change sees the
       skills after its next start. Hermes's `skills_list` and `skill_view`
       read the directory on every call, but the skill index in a running
       session's prompt does not show a skill granted after the session
       started (seen in the acceptance): Hermes finds it when it lists its
       skills, and in the index after it starts again;
     - "read-only" means the copy is restored from the platform before every
       turn and never written back: the files are 0444 (0555 when
       executable) in 0555 directories, and Hermes's curator does not touch
       external skills, but the sandbox user owns them and could change the
       modes. The relay replaces the directory's entries without following
       links, so a link the agent put there never makes it remove anything
       else;
     - an unchanged copy of a platform skill in Hermes's own skills is no
       draft; a changed copy is a draft of a new skill, since a draft updates
       only the skill its own directory became. Edits to the hydrated copy
       itself are never drafted, unlike the design above, which diffed
       against what was hydrated;
     - the skills' scripts run in Hermes's terminal under the sandbox's
       egress mode, not the skill's `network_policy`.
6. **Workflow node**, implemented. An agent definition has a kind: `agent`
   or `hermes`, fixed at creation. A Hermes Agent definition runs workflow
   nodes; it takes no skills or knowledge bases (Hermes brings the platform's
   tools and its own skills), its system prompt becomes its instructions, and
   it never supervises a workflow, which plans and synthesizes in the text
   protocol (refused on save and on template import). Accepted in the browser
   by `./smoke-test-hermes-ui.sh`: a workflow whose one node is a Hermes Agent
   answers through Hermes in the conversation's sandbox. How it works:
   - a Hermes node runs on the organization's Hermes model, not the
     definition's: a running sandbox keeps the model it started with, as in
     chat. The run's snapshot carries the kind and that model; the run does
     not start without the operator switch, both Hermes capabilities and an
     organization Hermes model, and every step checks them again, with the
     model still the organization's, before it runs (synthesis too, as for
     every node agent);
   - `AgentWorkflow` takes the Hermes path only for a Hermes definition,
     behind `workflow.patched("hermes-node-v1")`: it registers the node's
     execution, hands the node to the conversation's Hermes session with the
     brief and the predecessors' outputs as input (no chat history), and
     waits for its answer, asking or skipping Hermes's questions and keeping
     the runner alive as a chat turn does. The node's result is Hermes's
     answer; images and sources Hermes produces stay in its workspace. A
     failed run fails the node with the session's code, so `on_failure`
     edges apply; a cancelled run cancels the node's Hermes turn, so the
     session moves on;
   - nodes of one chat turn share its turn id, so each node execution has its
     own key in the session (`{turn}:{execution}`) for its queue entry, its
     run and its answer, and the binding carries the run, phase and node, so
     the node's model calls are billed to its step. A node's progress stays
     off the chat stream;
   - parallel Hermes nodes of one conversation run one after another: the
     session runs one turn at a time and queues the rest, chat turns
     included. A node waits for the nodes ahead of it within its budget,
     which covers a workflow's four parallel nodes (four turn deadlines and a
     quarter of an hour).

Recommended v1 for real use: phases 1–4. Memory, drafts and nodes follow.

### Files and charts

Hermes makes charts and files in its terminal (the image carries matplotlib,
pandas and openpyxl, pinned by hash in `hermes-sandbox/requirements-data.txt`)
and gives them to the user with the bridge's `attach_file` tool and a path in
its workspace:

- the bridge reserves a `file_attach` operation of the bound execution, at most
  ten per turn, counted from the turn's operations under its lock rather than in
  a turn column the archive would carry. It commits, then starts a
  `HermesFileAttachWorkflow` on tool-worker's queue, keyed by the operation;
- tool-worker reads the file through skill-executor, whose `hermes-relay
  file-read` takes only a regular file in the workspace with one link, reached
  component by component without following links, outside what snapshots leave
  out, at most 20 MiB, and streams it in base64 lines that must add up to the
  size and SHA-256 of its closing line. The bytes go straight to the backend's
  runner-only upload route, never into workflow history;
- the backend recognises the bytes, never trusting the name: PNG, JPEG and WebP
  become a generated conversation image of the turn (outside the image budget),
  anything else must be one of the conversation file types and becomes a
  conversation file;
- the bridge waits `APP_HERMES_FILE_TIMEOUT_SECONDS`; a call that gives up fails
  its operation, so bytes arriving later are refused instead of being attached
  twice when the model tries again;
- the runner never sees Hermes's tool results, so when an answer is persisted
  the backend shows every image of its turn after the ones the runner claimed,
  and every file of the turn, which the message lists with its name, type and
  size. Workflow nodes share the turn, so a Hermes node's files reach the final
  answer.

### Files the user gives Hermes

In a Hermes conversation the user attaches files to a message (spreadsheets,
CSV, documents; the conversation file types; and PNG, JPEG or WebP pictures,
which only uploads may be, recognised and bounded as image uploads) and Hermes
works on them in its terminal: its code reads a picture, the model does not
see it, for example to build a pivot table and a presentation from several
workbooks. The image carries python-pptx, python-docx and pypdf for that:

- the composer uploads each file (`POST /v1/conversations/<id>/files`) and the
  message carries their ids; only a Hermes turn takes them. Sending claims them
  for the turn, the user's message lists them, and each gets a workspace name in
  `/workspace/inputs/`: its own, numbered when an earlier upload has it, never
  changed once given;
- the runner receives the turn's text with a note naming those paths, so the
  stored message keeps the user's own words;
- before every turn, after the platform skills, the session workflow runs
  `hermes_sync_inputs` (patch `hermes-inputs`): tool-worker lists the
  conversation's claimed uploads from the backend, asks skill-executor what the
  sandbox holds (`hermes-relay inputs-list`: regular, singly linked files with
  their SHA-256), and writes only what is missing or changed, each checked
  against the recorded size and SHA-256, through a staging folder that
  `hermes-relay inputs-write` moves into the inputs folder, read-only. A
  replacement sandbox gets them again. The files never enter workflow history;
- a turn whose files cannot be written does not run: it ends with
  `hermes_inputs_unavailable` and a reason the user reads, since its text names
  files that would be missing;
- the bounds (ten files a message, twenty and 200 MiB a conversation, 20 MiB a
  file) keep one sync within its ten minutes and a snapshot within its limit.
  Inputs are part of the workspace and its snapshots.

### Web applications

Hermes can build a web application prototype, check that it works and hand
over its code. The image carries what that takes, all verified:

- Node.js (the official LTS release, pinned by hash per architecture) with npm,
  next to Python. npm reaches `registry.npmjs.org` through hermes-egress in
  `packages` mode with the same HTTPS proxy as pip, set in the environment by
  `start.sh`, never in a file the workspace or its snapshots keep. Global
  installs go to `/workspace/.local`; native addons cannot build (no
  compilers);
- Debian's Chromium headless shell (`/usr/bin/chromium-headless-shell`) with
  fonts that cover Cyrillic, and Playwright for Python.
  The agent starts the app on 127.0.0.1 and checks it in headless Chromium
  (`--no-sandbox`, `--disable-dev-shm-usage`, a profile in `/tmp`), clicking
  through it and attaching screenshots. npm packages never download a browser
  the proxy would refuse (`PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD`,
  `PUPPETEER_SKIP_DOWNLOAD`); they use the system Chromium. Hermes's own
  browser toolset stays off;
- package caches (pip, npm) live in `/workspace/.cache`, which snapshots leave
  out, so a snapshot holds one copy of what is installed. A typical
  `node_modules` (tens of thousands of entries) fits the snapshot bounds; over
  them the last snapshot that fit stays the latest;
- nothing in the sandbox is reachable from outside: the user runs the app from
  the ZIP Hermes attaches, whose README says how. Code files keep their names.
- Hermes is told all this on every turn: skill-executor's `SANDBOX_INSTRUCTIONS`
  reach its system prompt as the runs API's `instructions` (the relay forwards
  them), which a turn Hermes runs itself uses, and as the generated config's
  `agent.system_prompt`, which a turn its session worker runs reads instead.
  The notes are neutral about the network mode, which the organization picks.

### Idle sessions

The session workflow retires an idle sandbox after its idle time (1800 s) and
then closes the session with the conversation runner's token. That token
lives thirty minutes and only chat requests renew it, so by then it has
expired, and the close is refused: tool-worker leaves it at that instead of
retrying. The backend closes the sessions left open itself: every
maintenance interval it closes sessions open without a turn for
`APP_HERMES_SESSION_IDLE_CLOSE_SECONDS` (2700, at least 900) in every
organization, through the bounded `close_idle_hermes_sessions` function, which
clears the binding and revokes every token as closing does. A session bound to
a turn is never closed this way.

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

- Settled in 3b: code skills are not reachable through the bridge; they arrive
  with phase 5's hydrated skills and run in Hermes's own terminal.
- Default resource limits, lifetime limits and the capacity caps.
- Egress defaults: bytes per turn and concurrent connections, and whether the
  default package list should also cover npm or Git hosts.
- Settled in 6: a conversation's Hermes nodes run one at a time, queued in its
  one session, each billed through its own binding.
