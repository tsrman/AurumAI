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
- Service lint/types/tests, frontend tests/builds, migration checks, image builds
  and offline acceptance-harness contracts.

## Current release preparation

- AurumAI branding, AGPL-3.0-only licensing and third-party attribution.
- English public documentation, comments and docstrings.
- Portable examples/bootstrap with persistent secrets and explicit provider models.
- Manifest-selected export without private history, runtime data or credentials.
- Public-fork CI and redacted independent secret scanning of the exported candidate.
- Clean-install verification from the candidate rather than the private checkout.

Source preparation is complete and verified from an independent snapshot. Public
repository creation, the initial commit and publication await the chosen destination
and explicit authorization. Provider/Kubernetes/long-deadline gates below remain separate.

## Remaining product and verification work

- Integrate Hermes Agent as a universal system agent within the existing
  conversation runtime, tools and workflow orchestration.
- Add image generation and editing through an OpenAI-compatible API, with
  configurable providers/models and support for image inputs and outputs in chat.
- Loop wall-clock budgets; nested/overlapping loop bodies and confirmation nodes
  inside a loop remain outside the current graph contract.
- Broader runtime/provider/load coverage, with fresh Kubernetes CNI/SSRF acceptance
  distinct from unit/Helm checks.
- Stable external public-search acceptance and a mixed long real-provider dialogue
  with MCP, questions, web, complete REST history and reload.
- Operator rollout hardening and operational documentation across deployment-specific
  credentials, model revisions, network enforcement and backup/recovery.
- Removal of remaining upstream development-tool advisories when patches are available.

Default-off features stay gated until their prerequisites and enforcement checks
are satisfied. A historical acceptance pass does not automatically close a fresh
release gate. See [testing](docs/TESTING.md) and [interactive coverage](docs/testing/interactive-web-coverage.md).

Authorization UI acceptance: `./smoke-test-authorization-ui.sh` verifies both Keycloak audiences and live resource grants. Run it exclusively with trace capture disabled; keep the private journal if exact-owned recovery fails. See the testing guide for prerequisites.
