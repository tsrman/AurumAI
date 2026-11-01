# API reference

AurumAI has two HTTP planes served by the same backend code with different audiences:

| Plane | Port | Audience | Used by |
|---|---|---|---|
| **Business API** | 8000 | Chat app users | `business-ui`, runners, workers |
| **Admin API** | 8001 | Administrators | `admin-ui` |

Both authenticate with Keycloak access tokens (`Authorization: Bearer ...`). Each plane
accepts only its own audience, so a chat token does not open the Admin API. Responses
are mostly wrapped as `{"data": ..., "meta": ...}` (lists add `pagination`), and errors
carry a bounded `code`.

Request and response schemas are not repeated here. With `APP_DEBUG=true` each plane
serves interactive OpenAPI documentation at `/docs` and `/redoc`; it is disabled otherwise,
so for a production deployment read the routers in
`business-backend/src/routers/` or run a development instance.

Paths below are relative to the plane's host. The business routes are mounted under
`/v1`.

## Conventions

- **Rate limits:** per user, 300 ordinary requests and 30 chat streams per minute by
  default; over the limit the API answers 429.
- **Quota:** model calls count against the organization's monthly request and token
  limits; when spent they are refused.
- **Capabilities** gate actions; **grants** gate resources. A missing capability answers
  403; a resource you cannot see answers 404.
- **Service-only** routes (everything under `/v1/internal/*` and
  `/v1/hermes/chat/completions`, the Hermes MCP bridge and the preview checks) are for
  the platform's own services. They accept service credentials or session tokens, not
  browser tokens, and are not a client API.
- Health: `GET /health` (liveness), `GET /ready` (dependencies), `GET /metrics`.

## Business API (port 8000)

### Conversations and chat

| Method and path | Purpose | Needs |
|---|---|---|
| `GET /v1/chat/agent-definitions` | The administrator's agents the user may chat with now: granted, active, ordinary (not Hermes-kind), on a model the user may use | `chat.send` |
| `GET /v1/chat/agents` | Agents the user may chat with (Hermes only if a Hermes turn would be accepted) | `chat.send` |
| `POST /v1/chat/stream` | Send a message and stream the answer (SSE); `agent: "hermes"` selects Hermes Agent; `agentDefinitionId` runs the turn on an administrator's agent (its model, instructions, skills and knowledge bases; only one of `agent`, `agentDefinitionId` and `workflowId` may be given). A missing, archived or ungranted agent is 404, one whose dependencies the user cannot use is 403, a Hermes-kind agent is 422 | `chat.send` |
| `POST /v1/chat/completions` | Completion route that the conversation runner calls with its session token; also accepts a user token | `chat.send` |
| `POST /v1/chat/{id}/cancel` | Cancel the answer in progress | owner |
| `GET /v1/conversations`, `POST /v1/conversations` | List and create | `chat.send` |
| `GET /v1/conversations/{id}`, `PATCH`, `DELETE` | Read, rename or update, delete | owner |
| `POST /v1/conversations/{id}/messages` | Append a message | owner |
| `PATCH /v1/conversations/{id}/messages/{turn}/score` | Rate an answer | owner |
| `GET /v1/conversations/{id}/turns/active`, `.../turns/{turn}`, `.../turns/{turn}/events` | Reload and resume a turn | owner |
| `GET /v1/conversations/{id}/turns/{turn}/sources` | Web sources cited in a turn | owner |
| `POST /v1/conversations/{id}/executions` | Run an agent or skill execution | owner |
| `GET /v1/models` | Models the user may use | `chat.send` |

### Attachments, images and files

| Method and path | Purpose |
|---|---|
| `POST /v1/conversations/{id}/attachments` | Attach a document for retrieval (202, then indexed) |
| `POST /v1/conversations/{id}/images`, `DELETE .../images/{image}` | Upload and remove a picture for a vision model |
| `GET /v1/conversations/{id}/images/{image}/content` | Download a picture |
| `POST /v1/conversations/{id}/files`, `DELETE .../files/{file}` | Upload and remove a file for Hermes's workspace |
| `GET /v1/conversations/{id}/files/{file}/content` | Download a file Hermes produced or you uploaded (always as an attachment) |

### Knowledge and documents

| Method and path | Purpose | Needs |
|---|---|---|
| `POST /v1/knowledge-bases` | Create | `workspace.knowledge.create` |
| `GET /v1/knowledge-bases`, `GET .../{id}`, `DELETE .../{id}` | List, read, delete | grant / owner |
| `POST /v1/documents`, `POST /v1/documents/upload` | Register or upload a document (202) | `workspace.knowledge.create` |
| `GET /v1/documents`, `GET .../{id}`, `DELETE .../{id}` | List, read, delete | grant |
| `POST /v1/knowledge-bases/{id}/search` | Search a knowledge base | grant |

### Agents, skills and workflows

| Method and path | Purpose | Needs |
|---|---|---|
| `GET /v1/agent-definitions`, `POST /v1/agent-definitions` | List (`includeArchived=true` adds archived agents) and create agents | `workspace.agents.create` to create |
| `GET/PATCH/DELETE /v1/agent-definitions/{id}`, `GET .../{id}/execution` | Read, edit, delete an agent; its execution settings | grant / owner |
| `POST /v1/agent-definitions/{id}/archive`, `POST .../{id}/unarchive` | Archive or restore an agent (idempotent); an archived agent is hidden from lists, refused for new use and keeps its history | write grant |
| `GET /v1/skills/`, `GET /v1/skills/{id}` | Skills the user may use | grant |
| `GET /v1/workflows`, `GET /v1/workflows/{id}` | Workflows the user may run | grant |
| `GET /v1/workflow-runs/{id}` | A run's state | owner |
| `POST /v1/workflow-runs/{id}/cancel` | Cancel a run (202) | owner |
| `POST /v1/workflow-runs/{id}/nodes/{node}/decision` | Approve or reject a confirmation node | owner |

### Questions

| Method and path | Purpose |
|---|---|
| `GET /v1/conversations/{id}/questions` | Open questions of a conversation |
| `POST /v1/conversations/{id}/questions/{question}/answer` | Answer one |
| `POST /v1/conversations/{id}/turns/{turn}/cancel` | Cancel a turn waiting on a question |

### User settings and usage

| Method and path | Purpose |
|---|---|
| `GET /v1/settings/me`, `PATCH /v1/settings/me` | Personal settings |
| `GET /v1/usage/me` | Your usage against the organization's limits |
| `GET /v1/hermes/memory`, `DELETE /v1/hermes/memory` | Your Hermes memory |
| `GET /v1/hermes/skill-drafts`, `DELETE /v1/hermes/skill-drafts/{id}` | Skills Hermes wrote for you; withdraw one |
| `POST /v1/conversations/{id}/previews/{preview}/ticket` | Mint the one-time ticket that opens a Hermes preview (needs both Hermes capabilities) |

## Admin API (port 8001)

Every route needs the `org_admin` role unless noted.

| Method and path | Purpose |
|---|---|
| `GET /v1/admin/users` | Users and their roles |
| `GET /v1/admin/groups/`, `POST`, `PATCH /{id}`, `DELETE /{id}` | Groups |
| `GET /v1/admin/authorization/capabilities` and `/subjects` | Capability catalog and subjects |
| `GET/PUT /v1/admin/authorization/capabilities/organization` | Default permissions |
| `GET/PUT /v1/admin/authorization/capabilities/groups/{id}` | A group's capabilities |
| `POST /v1/admin/authorization/preview/users/{id}`, `.../groups/{id}` | Preview effective access |
| `GET/POST /v1/admin/authorization/grants`, `PUT .../grants/reconcile`, `DELETE .../grants/{id}` | Resource grants |
| `GET /v1/admin/analytics/usage` | Usage report |
| `GET /v1/admin/models`, `PUT /v1/admin/settings/models` | Model catalog and defaults |
| `GET/PUT /v1/admin/settings/images` | Image models and vision models |
| `GET/PUT /v1/admin/settings/hermes` | Hermes model and egress mode |
| `GET /v1/admin/hermes/skill-drafts`, `GET .../{id}`, `POST .../{id}/approve`, `POST .../{id}/reject` | Review skills Hermes wrote |
| `GET /v1/skills/`, `POST /v1/skills/`, `PATCH/DELETE /v1/skills/{id}` | Manage skills |
| `POST /v1/skills/discover-mcp-tools`, `POST /v1/skills/code`, `PUT /v1/skills/code/{id}/bundle` | Discover MCP tools; create and replace code skills |
| `POST /v1/admin/workflows/validate`, `GET/POST /v1/admin/workflows`, `GET/PUT/DELETE /v1/admin/workflows/{id}` | Workflow definitions |
| `GET/POST /v1/admin/workflow-templates`, `GET/PUT .../{id}`, `POST .../{id}/instantiate`, `POST .../{id}/archive` | Workflow templates |
| `GET /v1/admin/workflow-runs/{id}` | Run diagnostics |

The Admin API also serves the agent-definition and knowledge-base routes above, so the
admin app can manage them.

## Authentication

Sign in uses OpenID Connect through Keycloak (realm `aurum-ai`). Browser clients are
`business-ui` and `admin-ui`; each issues tokens with its own audience. The runner,
tool workers and Hermes sandboxes use **session tokens** minted by the backend that
carry identity only, have their own audience, and are valid only while their session
or turn is open. Authorization is evaluated live on every request.

For local scripts, a development token can be fetched with the password grant against
the Keycloak token endpoint using the `user` or `admin` fixture accounts. Do not do that
outside development.
