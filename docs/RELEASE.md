# Preparing a public AurumAI source release

A public source candidate is a reviewed snapshot with a new Git history. Never
mirror the private repository or change its visibility as a shortcut. The source
manifest includes required build/test/deployment inputs and excludes private
history, journals, runtime state, env files and design caches.

## Build the candidate

Install maintenance dependencies and choose a new directory outside the source:

```bash
cd scripts && npm ci
cd ..
python3 scripts/prepare-oss-release.py export --source . --destination /tmp/AurumAI-candidate --manifest release/oss-manifest.json --report /tmp/AurumAI-inventory.json
python3 scripts/prepare-oss-release.py verify --candidate /tmp/AurumAI-candidate --manifest release/oss-manifest.json --inventory /tmp/AurumAI-inventory.json --report /tmp/AurumAI-checks.json
```

The CLI rejects an existing destination/report, unsafe paths/symlinks, prohibited
artifacts, missing required files and detected secrets. Verification compares the
file set/content to the recorded inventory and checks that only the UI catalogs
contain Russian text. Reports are
private files and contain findings metadata, never matched secret values.

Source changes/new files must be reviewed and admitted to the manifest/Git index.
Do not automatically collect arbitrary untracked working-directory data.
Independent secret scanning is mandatory; the custom checks alone are not a claim
of comprehensive credential discovery.

## Independent review

Gitleaks 8.30.1 is pinned in `release/tools.json`; verify the official distribution
checksum before running it. CI scans the exported directory with `--redact`, not
private Git history. Review archive contents, replay fixtures, dependency/license
inventories, generated example configuration and all remaining findings.

Install dependencies in the candidate itself, initialize a separate local `main`
checkout for Git-tracked tooling when needed, and run service/tests/builds/Helm/
Compose/documentation checks there. Do not symlink original virtual environments
or node_modules as evidence that a clean clone works.

Use an isolated owned Compose project and fresh disposable volumes for startup/
browser/deterministic tool-workflow acceptance. Preserve shared resources and private
incident evidence. Exact cleanup must succeed before declaring a live gate passed.

## Publication checklist

1. Freeze the file/hash inventory; any edit invalidates affected evidence.
2. Confirm AGPL-3.0-only project ownership notices and upstream attribution.
3. Confirm English public docs/comments, working references and redacted scans.
4. Record fresh tests/builds/startup results and explicit blocked/failed coverage.
5. Obtain the intended repository owner/name and explicit publication authorization.
6. Create a new public repository on `main` with one reviewed initial source commit.
7. Set actual source URLs/build provenance; configure and test private vulnerability
   reporting and community reporting paths before announcing the repository.
8. Enable the prepared read-only/fork-safe CI, then create tags/releases only for
   artifacts that have actually passed the declared checks.

Do not invent hosted repository/image URLs, announce unpublished tags, or push an
unreviewed private branch. Operator data/credentials/archives are never release
assets. See [security](../SECURITY.md), [third-party notices](../THIRD_PARTY_NOTICES.md)
and [testing](TESTING.md).

The snapshot exporter requires Linux with `renameat2(RENAME_NOREPLACE)` support. It fails closed on unsupported hosts or filesystems, so an existing or concurrently created destination is never replaced. Keep private reports outside both the source and candidate trees.

Run the pinned Gitleaks binary with `--config <candidate>/.gitleaks.toml --max-archive-depth 2 --max-decode-depth 2 --redact`. The configuration extends all upstream rules and permits only exact demonstration values and non-credential code phrases; it does not exempt directories or test files. The scanner regression test also verifies detection of a token inserted on an otherwise permitted fixture path.
