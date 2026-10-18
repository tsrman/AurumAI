# Contributing to AurumAI

AurumAI welcomes fixes, documentation improvements, tests, and focused features.
Project-owned contributions are distributed under AGPL-3.0-only; preserve upstream
license and copyright notices for third-party material.

## Before changing code

Read [the architecture](docs/ARCHITECTURE.md) and [maintainer guidance](AGENTS.md).
Use a feature branch and describe the user-visible problem. Discuss changes to
workflow execution, authorization, public interfaces or data retention before
implementing them. Keep unrelated refactors and dependency upgrades separate.

All documentation, comments, docstrings and source strings must be English. The
only Russian text in the repository is the `messages/ru.json` UI catalogs; tests that
need Cyrillic data build it from escapes. Translating text must not change
identifiers, payloads or protocol data.

Both UIs are localized (English and Russian) with next-intl. Add user-visible text
to `messages/en.json` and `messages/ru.json` of the affected application with the
same key in both files, never as a literal in `src/`.

## Development and checks

Start with [QUICKSTART](QUICKSTART.md). Python services use Python 3.12 and uv;
frontends use Node 22 and npm. Install dependencies with frozen locks. The
repository's maintenance checks use `cd scripts && npm ci`.

Run the checks for each affected service and the relevant harness tests. See
[testing](docs/TESTING.md) for commands, separate PostgreSQL test databases,
frontend builds and optional live acceptance. Use a failing behavioral regression
before fixing a bug. Prose-only edits need link/language checks, not tests that
merely compare wording.

## Pull requests

- Explain the problem and resulting behavior, then provide verification evidence.
- State any unavailable prerequisites or remaining coverage gaps explicitly.
- Preserve migration history and Temporal patch markers; include replay evidence
  when changing workflow commands or child inputs.
- Exercise authorization using live grants and tenant isolation, not an admin-only
  happy path. Browser and service session tokens have different audiences.
- Keep secrets, runtime data, browser traces and private recovery journals out of
  source, logs, issues and attachments.

CI runs without paid-provider credentials. Real-provider, Kubernetes network, and
exclusive organization-mutating tests remain manual/opt-in. A skipped integration
test is not evidence that its deployment boundary passed.

Maintainers review compatibility, operational recovery, license provenance and
scope as well as test results. Contributions may be revised or declined when
they weaken those guarantees.
