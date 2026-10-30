# Data lifecycle acceptance

The archive lifecycle spans business PostgreSQL, MinIO, private RAG metadata,
original files/dense vectors and Temporal receipts. A source/database unit pass
does not prove the complete export/purge/restore path.

## Required evidence

- A fresh owned isolated fixture with recorded org/conversation/KB/file/job identities.
- Independent initial checks of full conversation history, UUIDs, grants, typed
  business rows, source-file checksum, vector values/schema/dimension and index receipt.
- Verified archive objects before deleting business/source/vector payload.
- Normal UI/API hide archived conversations after purge; billing/usage/audit
  identity/ownership remain intact.
- Operator export produces a new private directory and checked objects only.
- Restore preview/apply uses the same archive and a strictly newer generation;
  history returns after reload with original UUIDs/rights/files/vectors/receipts.
- Restoration performs no embedding/LLM call or historical workflow replay.
- Scoped IAM and network controls enforce separate ordinary/lifecycle credentials.

Tests must cover active work/lost replies, source changes, FK/unique collisions,
archive corruption and size bounds. Discovery coverage includes recent restored
activity, strict retention boundary, locks and blocked-candidate rechecks under
sustained eligible arrivals.

## Conversation images and files

`./smoke-test-archive-images.sh` runs on the same disposable project
(`scripts/fixtures/data-lifecycle-compose.yml`) without Keycloak or RAG, since a
conversation without attachments never calls RAG. It builds the backend and RAG
images from the checkout, checks bucket IAM including the image account (Get, Put
and Delete under `conversation-images/*` only, no listing, no other prefix, no
archive bucket), prepares an old conversation with a generated image, an edit of it,
an upload and a CSV file an agent attached (`prepare-data-lifecycle-images.py`), and drives the real data-worker
image through archive and restore with its operator login, archive account and image
account. `check-data-lifecycle-images.py` independently verifies rows and bytes
before, after the purge (platform objects gone, archived copies with the original
SHA-256) and after the restore (image and file rows, edit reference and bytes back, newer
generation). A run without image storage must leave the conversation unclaimed. The
project lives in tmpfs and is always removed. The fixture has no egress guard; the
Compose allowlist entry for image storage is covered by `scripts/tests` only.

## Fixture/recovery tooling

The maintained `scripts/*data-lifecycle*` helpers use private journals and exact
resource identities. Read each helper's prerequisites; never point a fixture at
unowned production resources or rerun a mutation whose original reply is uncertain.
Storage/business checkers remain independent: matching source values is not proof
of correct manifest identity or restore generation.

Use the printed exact-owned recovery procedure. Preserve journal, snapshot,
operation keys and immutable receipt hashes on failure. Do not replace Temporal
closure/remote-operation proof with prefix SQL or forced receipt updates. A
missing established history is a block; starting a replacement is not recovery.

Membership fixtures snapshot before mutation and preserve durable uncertainty when
Keycloak returns no definitive reply. Do not issue a compensating mutation that can
race an unfinished request. Browser reporting disables secret-bearing artifacts.

## Scope

Synthetic owned fixture acceptance validates the declared data graph and runtime.
It does not imply arbitrary transport-fault, paid-provider, model-quality or
Kubernetes CNI coverage. The new-archive gate remains operator default-off.
See [the operator runbook](../operations/data-archive.md),
[configuration](../CONFIGURATION.md) and [testing](../TESTING.md).
