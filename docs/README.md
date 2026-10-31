# AurumAI documentation

AurumAI is a self-hosted AI chat and agent platform for organizations: chat with
models and agents, knowledge bases, skills and tools, multi-agent workflows, and an
optional sandboxed agent (Hermes Agent). Start with the section for your role.

## I use the platform

- [User guide](guides/user-guide.md): sign in, chat, attach documents, knowledge bases,
  images, workflows, settings.
- [Hermes Agent guide](guides/hermes-agent.md): the agent with its own workspace.

## I administer an organization

- [Administrator guide](guides/admin-guide.md): users and groups, capabilities and
  grants, models, agents, skills, workflows, knowledge access, usage.
- [Workflows](WORKFLOWS.md) and [starter tools](STARTER_TOOLS.md).
- [Agent roles](AGENT_ROLES.md).

## I install and operate it

- [Installation and deployment](guides/deployment.md): Compose, Helm, proxy and TLS,
  optional features, production checklist.
- [Operations](guides/operations.md): health, upgrades, backups, troubleshooting.
- [Configuration reference](CONFIGURATION.md): every setting.
- [Air-gapped deployment](AIR_GAPPED.md).
- [Data archive, export and restore](operations/data-archive.md).

## I integrate or review it

- [API reference](reference/api.md).
- [Security model](reference/security.md).
- [Architecture](ARCHITECTURE.md) and the [Hermes Agent design](HERMES_AGENT.md).

## I contribute

- [Testing and coverage](TESTING.md) and [release preparation](RELEASE.md).
- [Contributing](https://github.com/tsrman/AurumAI/blob/main/CONTRIBUTING.md) and the
  [roadmap](https://github.com/tsrman/AurumAI/blob/main/ROADMAP.md).

## Conventions

Screenshots are of a development installation with the sample accounts. Names of
environment variables are given as they appear in `.env`; the backend reads the same
setting with an `APP_` prefix. Source code, comments and documentation are in
English; the interface itself is available in English and Russian.
