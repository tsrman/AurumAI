# Backup and recovery

What to back up on a Compose deployment, in which order, and how to restore it. The
procedures below were run against a live stack: all five databases and the roles were
dumped, then restored into a new PostgreSQL container started from the same image on an
empty volume. The migration revision and row counts matched and the application role
signed in with its old password. Your hosts, versions and data differ, so rehearse the
restore yourself before you need it.

For long-lived payload data that you archive on purpose, see
[data archive](data-archive.md); an archive is not a backup of the live database.

## What holds state

| What | Where | Notes |
|---|---|---|
| Application data, authorization, settings, workflows | PostgreSQL database `aurum_ai` (`postgres_data`) | Keycloak's accounts, roles and clients are **in the same database** |
| Knowledge-base metadata | `rag_platform` | |
| Workflow engine | `temporal`, `temporal_visibility` | Running and finished workflow histories |
| Tracing | `langfuse` (PostgreSQL) and ClickHouse (`clickhouse_data`) | Optional; traces only |
| Uploads, images, files, Hermes workspace snapshots, archives | MinIO (`minio_data`) | Rows in the database point at these objects |
| Knowledge-base vectors | Milvus and etcd (`milvus_data`, `etcd_data`) | Rebuildable from the documents |
| Secrets | `.env` | Without it the data cannot be used; see [secrets](secrets.md) |

`docker compose down` keeps the volumes. `docker compose down -v` deletes them; it is
never a repair step.

## Backing up

Take the database first and the objects second. A row can then never point at an
object the copy lacks; the reverse (an object nobody references) is harmless. Do not
run a backup while an archive sweep is deleting verified data.

1. **Databases.** One custom-format dump per database, from the running container:

   ```bash
   for db in aurum_ai rag_platform temporal temporal_visibility langfuse; do
     docker compose exec -T postgres pg_dump -U postgres -Fc "$db" > "backup/$db.dump"
   done
   docker compose exec -T postgres pg_dumpall -U postgres --globals-only > backup/globals.sql
   ```

   `globals.sql` carries the roles (`aurum_ai_app` and the knowledge-base role) with
   their password hashes. Dumps do not include them, and a new server needs them first.

2. **Objects.** Copy the MinIO volume while MinIO is stopped, or mirror the buckets with
   `mc mirror` from a client. Buckets are `aurum-ai-business`, `langfuse` and the
   archive buckets you configured.

3. **Configuration.** Copy `.env` and any override files into the same encrypted
   location. Record the image tags or the commit you ran.

4. **Check the copy.** A backup you have not restored is a hope. Restore the `aurum_ai`
   dump into a scratch database (below) after each change to the procedure.

## Restoring

### Into a scratch database (check a backup)

```bash
docker compose exec -T postgres psql -U postgres -c "CREATE DATABASE restore_check"
docker compose exec -T postgres pg_restore -U postgres -d restore_check --exit-on-error < backup/aurum_ai.dump
docker compose exec -T postgres psql -U postgres -d restore_check -Atc \
  "SELECT version_num FROM alembic_version; SELECT count(*) FROM users; SELECT count(*) FROM messages;"
docker compose exec -T postgres psql -U postgres -c "DROP DATABASE restore_check"
```

The restore brings the `vector` and `pg_partman` extensions with it; their packages
must be installed in the target server (the stack's PostgreSQL image has them). Compare
the revision and the counts with the live database.

### A whole deployment

1. Bring up PostgreSQL alone from the **same image version** as the backup
   (`docker compose up -d postgres`), on an empty `postgres_data`.
2. Load the roles, then replace each database with its dump:

   ```bash
   docker compose exec -T postgres psql -U postgres -f - < backup/globals.sql
   for db in aurum_ai rag_platform temporal temporal_visibility langfuse; do
     docker compose exec -T postgres psql -U postgres -qc "DROP DATABASE IF EXISTS $db" -c "CREATE DATABASE $db"
     docker compose exec -T postgres pg_restore -U postgres -d "$db" --exit-on-error < "backup/$db.dump"
   done
   ```

   A new server has already created `aurum_ai`, `rag_platform` and `langfuse` from the
   init scripts, which is why the databases are dropped and created again. Loading
   `globals.sql` reports that the roles `postgres` and `rag_app` already exist; that is
   expected, and the statements that follow set their attributes and passwords.
3. Restore `.env` unchanged. **The roles come back with the passwords they had**, so
   `APP_DATABASE_ROLE_PASSWORD` must be the old value, not a freshly generated one.
4. Restore MinIO's volume or mirror the buckets back.
5. Start the rest of the stack with the same profiles as before. Do not run a newer
   version first: migrations are applied on start, so restore with the version that took
   the backup and upgrade afterwards (see [operations](../guides/operations.md#upgrades)).
6. Check `/ready` on both APIs, sign in, open an old conversation with an attachment and
   send one message. Knowledge bases whose vectors were not restored are rebuilt from
   their documents; allow time for indexing.
7. For Hermes, restart `aurum-ai-hermes-egress-guard` and `aurum-ai-hermes-egress-1`
   after the backend is up.

### After a partial loss

- **Only MinIO is lost.** Conversations load but their files and images do not. Restore
  the buckets; do not edit rows to hide the gap.
- **Only the database is lost.** Objects without rows are unreachable. Restore the dump
  taken before the objects you still have, then expect objects newer than the dump to be
  unreferenced.
- **Keycloak data is wrong.** It is part of `aurum_ai`: restoring that database restores
  accounts and clients. Realm import runs only on a fresh database, so a realm change
  needs a reviewed upgrade, not a re-import.
- **Ambiguity.** If you cannot tell what a failed lifecycle or restore step did, stop and
  keep the volumes, dumps and logs as they are. Recovery evidence is more valuable than a
  quick retry.

## How often

Match the interval to the data you can afford to redo: daily dumps and a daily object
copy suit most teams. Keep several generations, keep one copy off the host, and store the
secrets copy separately from the data copy. Rehearse a full restore after every
upgrade of PostgreSQL or of the platform's major components.
