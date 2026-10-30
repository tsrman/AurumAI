# Data archive, export and operator restore

New background archive jobs are default-off. Configure infrastructure/roles and
verify observers before enabling them. Archived conversations are removed from the
normal UI/API; export and restore are operator procedures.

## Retention and eligibility

`APP_ARCHIVE_RETENTION_DAYS` defaults to **180**. Conversation age uses its latest
creation/update/message/restore activity; an old but recently used conversation is
not eligible. Audit payload uses record creation age.

A conversation with an open, unexpired Hermes Agent session is not eligible
(`hermes_session_open`). Session records are runtime state, not history: the purge
removes the conversation's closed and expired ones, the archive does not carry them
and a restore does not bring them back.

`APP_ARCHIVE_SWEEP_INTERVAL_SECONDS=86400` controls discovery timing and
`APP_ARCHIVE_BATCH_SIZE=100` is the built-in batch default. Values must be positive;
operators can override them. Turning `APP_ARCHIVE_ENABLED` off prevents new jobs,
not partition maintenance, terminal receipt observation or already-created recovery.

Discovery spends its bounded batch on age-eligible live payload. Unchecked candidates
are prioritized by reaching retention; checked blocked candidates by last check time.
New eligible arrivals do not continually displace blocked-candidate rechecks.
Infinite historical backfill has no bounded-wait guarantee; do not replace the
bounded batch with an unbounded drain.

Payload is written to private verified Parquet objects before source purge. Each
conversation image and each file an agent attached is copied into the archive as its
own object (`archives/v1/<org>/<archive>/images/<image>`,
`archives/v1/<org>/<archive>/files/<file>`), listed in the manifest with its
SHA-256 and size and verified like the parts; the platform copy is deleted only after
that verification, and a restore puts the verified bytes back at their original key,
reads them back and only then makes the conversation visible. Permanent
UUID registries, billing and usage remain in PostgreSQL; their presence is not proof
that archived payload is still available.

## Infrastructure preparation

1. Back up existing databases and stop writers for payload partition conversion.
   Install pg_partman 5.5.0/pgvector before applying migrations with a migration role.
2. Create separate login roles with `aurum_ai_lifecycle` membership for business
   worker/CLI and `rag_lifecycle` for private RAG. Ordinary `aurum_ai_app`/`rag_app`
   do not receive these rights; schema-owner roles remain separate/NOLOGIN.
3. Provide a private archive bucket/account scoped to `archives/v1/*`, allowing read
   and create. DeleteObject is unnecessary; readiness must not require unrestricted
   bucket write/delete or health objects.
4. For conversations with images, give the business data worker a second, separate
   S3 user on the platform bucket limited to `conversation-images/*` with GetObject,
   PutObject and DeleteObject (`APP_ARCHIVE_IMAGES_S3_*`, Helm
   `dataLifecycle.imagesS3`), and allow its endpoint in the data worker egress.
   For MinIO, for example:

   ```json
   {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
     "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
     "Resource": ["arn:aws:s3:::aurum-ai-business/conversation-images/*"]}]}
   ```

   Without it, conversations that hold images are never claimed.
5. Restrict normal API/RAG S3 accounts to their business bucket. MinIO root credentials
   in an ordinary API break archive separation; use scoped users or a separate endpoint.
6. Run private RAG with its lifecycle DB connection and independent service secret;
   never mount those routes in normal RAG API/public ingress. Business data worker
   needs only scoped DB, Temporal, archive S3, private RAG and (for images) the scoped
   platform image prefix, not Docker/Redis/LLM.
7. Run both observers continuously so exact terminal receipts are saved **before
   Temporal history expires**, even with new archives disabled. Verify dry-run and
   readiness before enabling new jobs.

Deployment keys/CIDRs are in [configuration](../CONFIGURATION.md) and
[Helm](../../charts/aurum-ai/README.md).

## Business CLI

Run in `business-backend` with an operator login in `APP_DATABASE_URL`, separate
archive S3 credentials and private RAG endpoint/secret. Supply a valid process secret
where configuration requires one; never copy the operator env into a normal API.

```bash
uv run python -m src.cli.archive_data list --org-id <ORG_UUID>
uv run python -m src.cli.archive_data run --org-id <ORG_UUID> --conversation-id <CONVERSATION_UUID>
uv run python -m src.cli.archive_data run --org-id <ORG_UUID> --conversation-id <CONVERSATION_UUID> --apply
uv run python -m src.cli.archive_data run --org-id <ORG_UUID> --audit
uv run python -m src.cli.archive_data run --org-id <ORG_UUID> --audit --apply
uv run python -m src.cli.archive_data resume --org-id <ORG_UUID> --archive-id <ARCHIVE_UUID> --apply
uv run python -m src.cli.archive_data export --org-id <ORG_UUID> --archive-id <ARCHIVE_UUID> --output <NEW_PRIVATE_DIRECTORY>
uv run python -m src.cli.archive_data restore --org-id <ORG_UUID> --archive-id <ARCHIVE_UUID>
uv run python -m src.cli.archive_data restore --org-id <ORG_UUID> --archive-id <ARCHIVE_UUID> --apply
```

`run` and `restore` preview without `--apply`. `list` displays job metadata. Preview
for attachments uses read-only RAG inspect; it does not promise that every RAG claim/
export check will pass. Apply verifies full RAG readiness before deleting sources.
Export writes checked objects into a **new private directory**; never place it in a
web server directory or turn it into a user-facing public download.

After a failure, locate the established job by exact conversation and preserve
its UUID/generation. Resume claimed/verified archiving with that archive identity.
Repeat an interrupted restore with the **same** archive/org identity. A new UUID
must not substitute for unresolved work.

Restore checks UUID/FK/unique conflicts, stages business rows invisibly, restores
RAG/files/vectors, then publishes business visibility last. Real constraints catch
concurrent conflicts before changing RAG. It does not overwrite foreign resources,
re-embed content, recreate shared collections, or execute historical workflows.

## Size and ownership bounds

Business cells are bounded at 1 MiB, serialized rows/SQL pages at 4 MiB, business
Parquet parts at 8 MiB, image objects at 100 MiB, RAG parts at 16 MiB and source files
at 1 GiB. Business SQL
cell/row admission happens before claim; page reads remain bounded. RAG content,
vector artifacts and source-file limits are checked during export after claim;
violations can leave a claimed job fenced while still preventing purge.

RAG source ownership must agree with canonical document keys and every reference.
Legacy ambiguous owners require a separately verified migration; absence of an owner
is not operator-global ownership. Original dense float32 vectors, collection schema/
dimension and index receipts are preserved. Restore gets a new generation; manifest
identity is immutable and old callbacks remain fenced.

## Archive format versions

The manifest and every Parquet part carry an archive version. Version 2 adds
`interaction_turns.image_count` and the `conversation_images` table; version 3 adds
the `conversation_files` table, whose bytes live in the conversation's
`conversation-images/<org>/<conversation>/files/` folder and use the same image
account; version 4 adds its `origin` and the files the user uploads, which have no
operation and live in `files/uploads/`, and may be pictures (a version-3 job
refuses a conversation holding one); version 1 is the format before images. A job keeps the version it was frozen with: version-1
jobs (no `_archive_version` in their inventory) still seal, verify, purge and
restore with the version-1 columns, and refuse to purge if a column version 1
leaves out no longer holds its default. Keys stay under `archives/v1/`.

## What blocks deletion

Age, runner absence and lease expiry do not prove completion. Block purge for active
operations, incomplete provenance/history, unknown children, changed sources,
corrupt archives or uncertain external-operation replies. Never start/terminate a
replacement workflow to fabricate missing established history.

S3/Milvus intent is recorded before submission. A lost response can mean the remote
system accepted a request that completes later. Retrying, object absence, matching
restored bytes, a stopped client or a terminal Temporal workflow do not prove that
an earlier remote request can no longer mutate the resource. The same holds for a
platform image delete whose reply was lost: restore only an archive whose purge has
completed, never right after a failed attempt.

Preserve exact org/KB/archive identities, generation, immutable receipt hash and
pending steps. Private `/internal/archive/inspect` and `/internal/archive/status`
use `X-Archive-Service-Secret`; raw responses/objects/secrets stay out of reports.
Reconcile only resources in that exact manifest/run with authoritative proof of
completion/cancellation of the original request. No universal force/ack shortcut
exists. Without that proof, safe continuation remains blocked; manual receipt
updates, intent deletion or prefix/org cleanup are not recovery.

## Coverage

[Data lifecycle acceptance](../testing/data-lifecycle.md) describes independent
SQL/source/vector/checksum checks and exact recovery. Docker/Helm checks do not prove
Kubernetes CNI behavior. Keep new-archive jobs gated until the target deployment's
prerequisites and operational procedures have been verified.
