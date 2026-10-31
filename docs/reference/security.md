# Security model

This page summarises the boundaries AurumAI enforces and the ones it leaves to the
operator. The deployment-specific procedures are in
[Installation and deployment](../guides/deployment.md); how to report a vulnerability is in
[SECURITY.md](https://github.com/tsrman/AurumAI/blob/main/SECURITY.md).

## Identity and sessions

- **Keycloak** authenticates people (OpenID Connect). The platform stores no passwords.
- Two browser audiences, one per plane: a chat token does not open the Admin API.
- The `org_admin` role lives in Keycloak. Group membership comes from the signed
  `groups` claim.
- Runners, tool workers and Hermes sandboxes use **session tokens** that carry identity
  only, have their own audience, and are valid only while their conversation, turn or
  session is open. No other route accepts a Hermes token.

## Authorization

- **Default-deny.** A new organization gets one capability (`chat.send`) and grants for
  global skills and models. Everything else is turned on by an administrator.
- **Capabilities** (what a person may do) and **resource grants** (which models, skills,
  knowledge bases, agents and workflows) are evaluated **live**: revocation applies to
  the next request, including turns already running.
- Resources are owned: conversations, files and previews are served only to their
  owner, and downloads are always attachments.

## Tenant isolation

- **PostgreSQL FORCE ROW LEVEL SECURITY** scopes every tenant table to the organization;
  the migration role is separate from the runtime role.
- Object storage keys are prefixed by organization and conversation.
- Per-conversation runner containers (or Pods) execute agent logic; they reach the
  backend, not the data stores.

## Code execution

- **Skill sandboxes** run uploaded code in isolated, resource-bounded containers with no
  network by default. Tenant MCP uses SSE; the unrestricted fetch server in the repository
  is a development fixture, not a tenant tool.
- **Compose** gives the backend Docker-socket authority, which is root-equivalent on the
  host. Use it only on a trusted host. **Helm** replaces it with namespace-scoped RBAC and
  network policies, in three separate namespaces.

## Network

- Web search and read go through a bounded `web-tools` service with URL and network
  policy; they are off by default.
- Browser-facing ports bind to loopback unless you change `PUBLIC_BIND_ADDRESS`.
  Docker-published ports bypass host firewalls, so publish through the proxy overlay.
- Internal service routes (`/v1/internal/*`) require service credentials and are not
  meant to be reachable from browsers.

## Hermes Agent

Hermes runs model-chosen commands, so its boundary is stricter than the others:

- **A sandbox's only way out is the `hermes-egress` guard.** On Docker each sandbox has its
  own internal network, which the backend never joins and sandboxes never share. On
  Kubernetes a default-deny NetworkPolicy lets a sandbox Pod reach only the guard (no DNS,
  no peer Pod, no platform service, no node, no cluster API) and lets only sandbox Pods
  reach the guard; this needs a CNI that enforces egress policy, and the sandbox Pod
  mounts no service-account token.
- **Egress is a mode**, `none`, `packages` or `public`, chosen by the organization within
  the operator's maximum (`HERMES_EGRESS_MAX_MODE`). Choose the lowest that does the job.
- **No provider credentials** reach the sandbox: only a session token that names a person.
  Model calls go through the backend's route, which applies grants and quota.
- **Resource caps:** CPU, memory, process and disk limits per sandbox; at most 16
  sandboxes at once and 4 per organization by default; idle sandboxes stop.
- **Skills Hermes writes do not run for anyone else until an administrator approves
  them.** Review every file.
- Sessions end after a maximum lifetime and are closed when idle; closing a session
  revokes its tokens and previews.

### Previews

A preview runs agent-written web code in the viewer's browser.

- A preview lives at an **origin of its own**, never the platform's. It opens with a
  ticket that lives 60 seconds and works once; the service exchanges it for a grant
  kept in a **host-only `__Host-` cookie** (HttpOnly, Secure, SameSite=Lax) valid for
  one hour.
- The service checks the grant with the backend on every new connection and re-checks
  upgraded connections (WebSockets) periodically, so ending the session cuts them. A
  check passes only for that preview's owner, with both Hermes capabilities, while the
  session is open.
- The service removes its cookie from requests, rewrites redirects to localhost, and
  drops cookies the app tries to set for other domains or for the preview cookie's name.
- The sandbox gains no network for it: traffic travels through the skill-executor as a
  byte stream to loopback ports 1024 and above.

**Recommendation:** serve previews from a registrable domain that is not the platform's
and not under it (for example `*.preview.example.org` when the platform is at
`chat.example.com`). The service cannot control what page script does in the browser:
on a shared parent domain such a script could set cookies for the parent that the
platform would then receive. The `__Host-` prefix protects the preview cookie itself,
not the platform's. Previews are off until the operator gives an origin.

## Data handling

- Prompts, documents, images and files are stored with conversations. Langfuse capture
  of full content is optional and off by default for content; enable it deliberately.
- Archived data is exported only by operator commands with their own roles; deletion
  follows verification, and ambiguity keeps the evidence.
- The source release excludes private logs, runtime data and credentials.

## Operator responsibilities

Replace development accounts and secrets, terminate TLS, restrict `/metrics` and
infrastructure ports, back up data, keep opt-in features behind their policies, and
choose Kubernetes when you need strong host isolation.
