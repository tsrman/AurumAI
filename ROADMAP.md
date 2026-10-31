# AurumAI roadmap

This public roadmap describes implementation and remaining work, not a guarantee
of production readiness or a claim that every runtime combination has been tested.
Detailed fresh verification belongs in the release report and CI.

## Implemented

- Keycloak identity, separate chat/admin API audiences, capabilities/resource
  grants, live revocation and PostgreSQL FORCE RLS.
- Streaming conversations with isolated Docker/Kubernetes runners and complete
  paginated persistent history.
- MCP skills, uploaded multi-round code skills and sandbox isolation.
- Knowledge bases, attachments, document parsing/indexing and hybrid retrieval.
- Durable multi-agent DAG workflows, versioned templates, routing, conditional
  branching, human approval and bounded loop-back edges.
- Previous-iteration context: every loop-body node in pass N+1 receives all terminal
  results from pass N only, with bounded outputs and permitted source identities.
- Durable model questions and bounded web search/read with citations, rollout
  switches, deadlines and exact-owned recovery.
- Optional background identity synchronization, monthly payload partitions and
  verified operator-only Parquet archive/export/restore.
- Admin UI in the chat app's shell: users and groups (default and per-group
  permissions, read-only Keycloak membership), agents (create, edit, delete),
  skills, workflows, knowledge bases, models and usage analytics, with access
  configured from each resource.
- Image generation and editing in chat through an OpenAI-compatible Images API,
  with separate organization models and an `images.generate` capability, and
  image attachments for chat models the organization marks as vision models;
  image calls count against the monthly quota and appear in usage analytics, and
  conversations with images are archived and restored with their bytes; workflow
  node agents create and edit images and receive attachments.
- Guided workflow authoring with runnable examples, and starter tools
  (`install-platform.sh`, CSV statistics and JSON formatting code skills).
- Hermes Agent as an opt-in system agent (Docker; Kubernetes sandboxes in progress): an isolated
  long-lived sandbox per conversation, organization-chosen network mode, platform
  tools over an MCP bridge, per-user memory, reviewed skills, files in both
  directions, vision, workflow nodes and previews of the apps it runs
  ([guide](docs/guides/hermes-agent.md), [design](docs/HERMES_AGENT.md)).
- Service lint/types/tests, frontend tests/builds, migration checks, image builds
  and offline acceptance-harness contracts.

## Public release

- AurumAI branding, AGPL-3.0-only licensing and third-party attribution.
- English public documentation, comments, docstrings and source strings; Russian
  text lives only in the UI locale catalogs.
- Portable examples/bootstrap with persistent secrets and explicit provider models.
- Manifest-selected export without private history, runtime data or credentials,
  with redacted independent secret scanning of the exported candidate.
- Public repository at github.com/tsrman/AurumAI with CI on every change, tagged
  releases starting at `v0.0.1`, and service images published to
  `ghcr.io/tsrman/aurum-ai-*` by the `publish images` workflow.
- Grouped Dependabot updates for Python, npm, Docker and GitHub Actions.

Provider/Kubernetes/long-deadline gates below remain separate from the release.

## Remaining product and verification work

- Hermes Agent on Kubernetes: the chart deploys the boundary and skill-executor runs the
  sandboxes (accepted on k3d); the repository's smoke script, the guides and
  `hermes-preview` in the chart follow.
- Loop wall-clock budgets; nested/overlapping loop bodies and confirmation nodes
  inside a loop remain outside the current graph contract.
- Broader runtime/provider/load coverage, with fresh Kubernetes CNI/SSRF acceptance
  distinct from unit/Helm checks.
- Stable external public-search acceptance and a mixed long real-provider dialogue
  with MCP, questions, web, complete REST history and reload.
- Operator rollout hardening and operational documentation across deployment-specific
  credentials, model revisions, network enforcement and backup/recovery.
- Removal of remaining upstream development-tool advisories when patches are available,
  and the major dependency upgrades Dependabot proposes (Node 26, zod 4, vitest 5,
  redis 8, mypy 2 and others) once each is migrated and tested.
- Editable group membership in the Admin UI: today sign-in synchronization replaces
  memberships from the Keycloak `groups` claim, so membership is managed in Keycloak.

Default-off features stay gated until their prerequisites and enforcement checks
are satisfied. A historical acceptance pass does not automatically close a fresh
release gate. See [testing](docs/TESTING.md) and [interactive coverage](docs/testing/interactive-web-coverage.md).

Authorization UI acceptance: `./smoke-test-authorization-ui.sh` verifies both Keycloak audiences and live resource grants. Run it exclusively with trace capture disabled; keep the private journal if exact-owned recovery fails. See the testing guide for prerequisites.
