# Operations

Running AurumAI after it is installed: health checks, monitoring, upgrades, backups,
data lifecycle and the problems operators meet most. For installation see
[Installation and deployment](deployment.md).

## Health and readiness

| Check | Meaning |
|---|---|
| `GET /health` on port 8000 or 8001 | Liveness: the process is up |
| `GET /ready` | The required application dependencies answer |
| `GET /metrics` | Prometheus metrics, including `http_requests_total`, `http_request_duration_seconds` and the image, web-tool and interaction-outbox counters |

A healthy `/ready` does not prove an optional feature works. Check each one's own
service: `docker compose ps`, then `docker compose logs <service>` for the one that
is failing. Keycloak and Temporal are slow to start.

Keep `/metrics` off the public internet; scrape it from inside the network.

## Observability

- **Langfuse** (`http://localhost:3001` locally) traces model calls. Capturing full
  prompts and documents is a deliberate privacy decision, separate from enabling
  telemetry.
- **Temporal UI** (`http://localhost:8082` locally) shows conversation, workflow,
  Hermes and tool workflows. Behind the proxy it sits behind basic auth.
- **Usage analytics** in the Admin app shows requests, tokens and images per user and
  model against the monthly limits (see the [administrator guide](admin-guide.md#usage-and-quotas)).

Never publish a complete environment dump or a browser trace that contains tokens.

### Audit and usage durability

Mutating API requests wait for their audit write before returning the HTTP response.
Audit records are not left in detached process tasks. A database failure is logged as
`audit_log_write_failed`; it does not change the outcome of a business mutation that may
already have committed. Audit and business writes are separate transactions, so audit-storage
failures still need operator attention.

Quota reservations, acceptance, completion and cancellation preserve PostgreSQL's configured
commit durability. Keep `synchronous_commit=on` for durable usage acknowledgement; the application
does not override it for faster counters. Atomic quota updates still minimize the aggregate
row's lock scope, and repeated request identities are counted once.

## Upgrades

1. Read the release notes and the [changelog](https://github.com/tsrman/AurumAI/blob/main/CHANGELOG.md) for migrations and new
   settings. Back up first (see below).
2. Pull the new source or images, then rebuild: `docker compose up -d --build`. The
   backend entrypoint applies database migrations with the migration role before it
   starts; the RAG service has its own migration job.
3. Rebuild the images that are built by profile when their sources changed:
   `docker compose --profile build build runner skill-sandbox` (and `hermes-sandbox`
   with the Hermes overlay).
4. Check `/ready`, then run one conversation end to end.

With Helm, migrations run through a dedicated hook, not on each replica. Roll back
API admission before stopping an identity worker, and keep the Temporal patch markers
across upgrades so running workflows replay. An existing Keycloak database keeps its
clients and mappers: apply realm changes through a reviewed upgrade, never by deleting
volumes.

Archive format versions are forward-only for jobs already started; an older job refuses
content its version cannot carry.

## Backups

Back up these; the stack's state is split across them. The procedure, its order and a
tested restore are in [backup and recovery](../operations/backup-recovery.md).

| What | Why |
|---|---|
| PostgreSQL (`postgres_data`) | Conversations, authorization, settings, workflows |
| MinIO (`minio_data`) | Uploads, images, files, Hermes workspace snapshots, archives |
| `.env` and any secret store | Generated secrets; without them the data cannot be used |
| Keycloak's data (in the `aurum_ai` database) | Accounts, roles, clients |
| Milvus (`milvus_data`, `etcd_data`) | Knowledge-base vectors (rebuildable from documents) |

`docker compose down` stops the stack and keeps volumes. `docker compose down -v` deletes
them: it is not a repair step.

## Data lifecycle

Payload tables are partitioned by month. Long-lived data can be **archived**: written as
verified Parquet and object copies, exported and later restored, by operator-only
commands. Archiving is off by default (`APP_ARCHIVE_ENABLED`); it needs its own database
roles and S3 accounts, and deletion happens only after verification. Conversations
holding images or files are archived with their bytes.

```bash
uv run python -m src.cli.archive_data list --org-id <ORG_UUID>
uv run python -m src.cli.archive_data run --org-id <ORG_UUID> --audit            # dry run
```

The runbook has roles, bounds, export and recovery: [data archive](../operations/data-archive.md).
An ambiguous deletion is not a successful cleanup: keep the recovery evidence.

## Identity synchronization

An optional worker (profile `maintenance`) synchronises Keycloak users and groups in the
background. Enable it with admission off, verify the groups mapper on both clients and
worker readiness, then turn admission on. Without it, sign-in synchronises a person's
groups when they log in.

## Scaling and limits

| Limit | Setting |
|---|---|
| Ordinary and stream rate per user | `APP_RATE_LIMIT_PER_MINUTE` (300), `APP_RATE_LIMIT_PER_MINUTE_STREAM` (30) |
| Runner pool | `RUNNER_POOL_SIZE`, idle reaping after 30 minutes |
| Workflow size | `WORKFLOW_MAX_NODES` (20), `WORKFLOW_MAX_PARALLELISM` (4) |
| Per turn | 10 questions, 4 open; 10 searches; 20 page reads; 4 images |
| Hermes sandboxes | 16 running, 4 per organization |
| Document upload | 25 MB |

The [configuration reference](../CONFIGURATION.md) has every setting.

## Database connections

Compose passes `APP_DB_POOL_SIZE`, `APP_DB_MAX_OVERFLOW` and `APP_DB_POOL_TIMEOUT` from the
root `.env` into both API containers. Recreate the APIs after changing them; if Hermes is enabled,
follow its [guard restart procedure](hermes-agent.md#troubleshooting) after recreating the backend.
Helm configures each API independently with `backend.databasePool` and `backendAdmin.databasePool`:
`size` (20), `maxOverflow` (40, including zero) and `timeout` (30 seconds). Every worker and replica
multiplies these values in the connection budget below.

Every API process may hold `APP_DB_POOL_SIZE + APP_DB_MAX_OVERFLOW` request connections (20 + 40
by default), plus one dedicated audit connection. Audit persistence does not compete with
connections held by request dependencies. Its pool has no overflow and uses `APP_DB_POOL_TIMEOUT`.
PostgreSQL refuses new connections beyond `max_connections` (100 by default). The budget is
the sum over everything that connects:

```
replicas x processes x (pool size + overflow + 1 audit)  +  RAG services  +  Temporal  +  Keycloak  +  Langfuse  <=  max_connections
```

On the Compose stack Temporal alone holds about 30, so the stack starts PostgreSQL with
`max_connections=200`, `shared_buffers=256MB` and `effective_cache_size=1GB` (PostgreSQL's own
defaults are 100 and 128MB); change them with `POSTGRES_MAX_CONNECTIONS`, `POSTGRES_SHARED_BUFFERS`
and `POSTGRES_EFFECTIVE_CACHE_SIZE` in `.env` and recreate the container. Raise `max_connections`
(and give PostgreSQL the memory for it), lower the pool, or put a connection pooler in front before
running several API replicas. A pooler in transaction mode is not tested with AurumAI here: the tenant context is set
per transaction, which suits it, but check prepared statements and your driver settings first.

A request that cannot get a connection within `APP_DB_POOL_TIMEOUT` seconds fails with a
`QueuePool limit` timeout. To see who holds them:

```sql
SELECT state, count(*) FROM pg_stat_activity WHERE datname = 'aurum_ai' GROUP BY 1;
```

Connections should be `idle` between requests. Many `idle in transaction` ones mean a request is
holding a transaction while it waits, which is a bug to report: the turn event stream used to do
exactly this for every open chat.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Stack is healthy but the first chat cannot start | The runner image is not built (`docker compose --profile build build runner`), or the API lacks Docker-socket access |
| A member cannot see a model or a new global skill | Access is default-deny: preview and apply `src.cli.provision_org --all` (see the [administrator guide](admin-guide.md#resource-access)) |
| Keycloak clients or mappers are out of date | The realm imports only on a fresh database; use a reviewed realm upgrade |
| The evaluator does not run | Set both `EVALUATOR_ENABLED=true` and an explicit `EVALUATOR_MODEL` |
| Knowledge search returns nothing | The documents may not be indexed yet; first model downloads take minutes |
| Users get "quota exceeded" | The month's request or token limit is spent: see Analytics |
| HTTP 429 | A user passed the per-minute rate limit |
| Web search or read refuses | `WEB_ENABLED`, the `web` profile, secrets and the enforcement checks; never open outbound access to make it work ([network enforcement](../operations/network-enforcement.md)) |
| A model vanished or a default shows as unavailable | The gateway no longer publishes the id: [changing models](../operations/model-changes.md) |
| Hermes: see its table | [Hermes troubleshooting](hermes-agent.md#troubleshooting) |

## Security hygiene

Rotate generated secrets deliberately; some are applied only once (the application
database role, the Keycloak administrator) and need more than an edit of `.env`: see
[secrets and credentials](../operations/secrets.md). Keep the development accounts out of
anything reachable by others. Report vulnerabilities as
described in [SECURITY.md](https://github.com/tsrman/AurumAI/blob/main/SECURITY.md).
