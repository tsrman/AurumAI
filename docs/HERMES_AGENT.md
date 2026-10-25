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

A Hermes turn is an external, non-replayable agent loop. It is wrapped the same
way code-skill sessions are.

- **Workflow.** `ConversationWorkflow` runs a child `HermesSessionWorkflow`. A
  workflow node does the same through its `AgentWorkflow`. The child is gated by
  `workflow.patched("hermes-v1")`, and replay tests cover histories from before and
  after the gate.
- **Activities.** All activities run on the `tools` queue and have bounded
  retries:
  1. `open_hermes_session` provisions the sandbox and hydrates `HERMES_HOME`.
  2. `run_hermes_turn` submits `POST /v1/runs` with an `Idempotency-Key` derived
     from turn and attempt. It then reads `/events`, heartbeats, and publishes
     safe progress.
  3. `close_hermes_session` reads back memory and skill drafts and destroys the
     sandbox.
- **Cancellation.** A cancelled turn calls `/stop`.
- **Failure.** If the sandbox or the activity's owner dies, the turn ends as
  `hermes_interrupted`. It is not resumed: Temporal records the platform's actions,
  not Hermes's internal state. This is stated in the user and operator docs.
- **History.** The final answer is persisted through `persist_assistant_message`
  as usual. The next turn starts from the complete persistent conversation history
  (`conversation_history` on `/v1/runs`), not from Hermes's `state.db`. History
  stays authoritative in PostgreSQL.
- **Session lifetime.** v1 opens a fresh session per turn: no warm sessions. A
  warm sandbox reused across turns of the same conversation is a later
  optimization, bounded by the idle TTL.

### Sandbox image and limits

- **Image.** A separate `hermes-sandbox` image installs Hermes at a pinned commit,
  with no `hermes update` and no build tools on the runtime path. It ships MIT
  attribution in `THIRD_PARTY_NOTICES`, and `AIR_GAPPED.md` lists it.
- **Resources.** CPU, memory and disk get their own settings, well above the
  code-skill defaults: initially 2 CPU, 2 GiB and a 1 GiB tmpfs `HERMES_HOME`.
  Absolute and idle TTLs are separate, and a turn has a wall-clock deadline.
- **Hermes configuration.** It is generated by the platform and read-only to the
  agent. The agent can overwrite the file, so the platform-side controls below
  must not depend on it.
  - `terminal.backend: local`: the sandbox is the boundary, with no nested Docker.
  - `agent.disabled_toolsets` covers Hermes's own web and browser tools, external
    memory providers, messaging platforms, cron/jobs and updates.
  - Delegation stays enabled only because subagents use the same proxy endpoint.
  - `direct_model_requests: false`.
  - The `max_concurrent_runs` limit is 1.
  - `approvals.mode` comes from the platform, not from Hermes defaults.

### Network path

This is the hardest boundary. The sandbox must reach exactly two platform
endpoints and nothing else:

- the model proxy;
- the platform tools MCP endpoint.

A new `hermes-egress` sidecar follows the `web-egress` pattern: nftables, a
default-drop policy and readiness that proves the rules are in place.

- On Docker, the sandbox runs on a dedicated internal network that has no NAT. The
  sidecar is the only other member.
- On Kubernetes, a NetworkPolicy keyed on the Hermes sandbox label allows egress
  only to the sidecar. The sidecar allows only the backend paths above.
- The existing `aurum-ai-skill-egress` network is never reused, and the backend
  never joins a network that sandboxes join.
- In v1 Hermes has no direct internet. Web access goes through the platform's
  `web.search` and `web.read` tools, so the existing SSRF policy, deadlines and
  citations apply.

Acceptance needs runtime evidence, not only static tests. From inside the sandbox,
probes to the internet, MinIO, PostgreSQL, Temporal, skill-executor and other
backend paths must fail, and the two allowed calls must succeed.

### Credentials in the sandbox

The model has a terminal. Anything in its environment or files can be read by the
model and by any prompt injection.

- **Session token.** The runner's session token never enters the sandbox. When a
  session opens, the backend mints a separate Hermes session token. It:
  - is bound to organization, user, conversation, turn and, where relevant,
    workflow run and node;
  - has the audience `hermes-session`;
  - is accepted only by the native-tools proxy route and the MCP bridge;
  - expires with the session TTL;
  - is revoked on close.
- **What a leak can do.** It allows model and tool calls for that one turn while
  the session lives, still subject to live grants and quotas. Nothing more.
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
- **`state.db` and session search.** These are not persisted in v1. Search over
  past conversations can later be offered as a platform tool scoped to the user's
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
   correlation. Tested against the deterministic gateway.
2. **Sandbox and network.** The `hermes-sandbox` image, its resources, the
   `hermes-egress` sidecar on Docker and Kubernetes, and runtime egress probes.
3. **MCP bridge.** Knowledge, web, images and questions. MCP skills and code skills
   come later in the same phase if the scope allows.
4. **Chat agent.** `HermesSessionWorkflow`, events, cancel, the picker, the
   capability and flag, the UI and acceptance.
5. **Memory and skill drafts.** Tables, hydrate and read-back, user settings and
   admin approval.
6. **Workflow node.** A `hermes` node type, billing and replay tests.

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
- **Reporting.** Which runtime combinations were actually exercised is reported
  separately.

## Open questions for review

- Session reuse across turns (warm sandbox) in v1, or a fresh session per turn.
- Whether code skills should be reachable through the bridge in v1, given that
  Hermes already has a terminal in its own sandbox.
- Default resource limits and the turn deadline.
