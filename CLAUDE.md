# AurumAI development instructions

Follow [AGENTS.md](AGENTS.md) for the complete repository conventions and safety/
compatibility boundaries. Start at [README](README.md),
[architecture](docs/ARCHITECTURE.md) and [testing](docs/TESTING.md).

Use a feature branch/worktree, uv for Python, npm for frontends and frozen locks.
Follow TDD for behavior changes and run relevant lint/type/test/build checks before
claiming success. Comments, docstrings, documentation and all source strings are
English; Russian text lives only in the `messages/ru.json` UI catalogs. Preserve
protocol/history content.

Commit/merge/push/publish only on explicit request. Public preparation exports a
reviewed source snapshot with a new history, not private logs, credentials, runtime
data or incident journals. Preserve exact ownership/replay/RLS/network boundaries,
and retain recovery evidence on ambiguity. Do not ask again for actions already
authorized within the current task.
