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

Back up these; the stack's state is split across them:

| What | Why |
|---|---|
| PostgreSQL (`postgres_data`) | Conversations, authorization, settings, workflows |
| MinIO (`minio_data`) | Uploads, images, files, Hermes workspace snapshots, archives |
| `.env` and any secret store | Generated secrets; without them the data cannot be used |
| Keycloak's database | Accounts, roles, clients |
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
| Web search or read refuses | `WEB_ENABLED`, the `web` profile, secrets and the enforcement checks; never open outbound access to make it work |
| Hermes: see its table | [Hermes troubleshooting](hermes-agent.md#troubleshooting) |

## Security hygiene

Rotate generated secrets deliberately and recreate both API planes afterwards. Keep the
development accounts out of anything reachable by others. Report vulnerabilities as
described in [SECURITY.md](https://github.com/tsrman/AurumAI/blob/main/SECURITY.md).
