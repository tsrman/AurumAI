# Secrets and credentials

Which secrets a Compose deployment holds, where each one lives, what it protects and
what rotating it takes. For Helm see the
[chart guide](https://github.com/tsrman/AurumAI/blob/main/charts/aurum-ai/README.md#secret-ownership);
the same questions apply, with the Kubernetes Secrets in place of `.env`.

## Where they come from

`./scripts/bootstrap-dev-env.sh` fills the empty secret keys of `.env` with
`openssl rand -hex 32` and keeps every value you already set. Keep `.env` out of
version control and out of backups that others can read.

Compose falls back to a **publicly known development default** for most secrets that
are left empty (Redis, MinIO, Langfuse, the application database role). An empty value
is therefore not a safe value: after bootstrap, check that none of the keys below is
empty, and never start a shared host from `.env.example`.

## Inventory

| Key in `.env` | Protects | Set by |
|---|---|---|
| `APP_SECRET_KEY` | Signs the identity-only tokens of runners, Hermes sessions and previews | bootstrap |
| `APP_DATABASE_ROLE_PASSWORD` | The `aurum_ai_app` database role the APIs run as (row-level security applies to it) | bootstrap, **applied once** by the first migration |
| `RAG_APP_DB_PASSWORD` | The knowledge-base service's database role | bootstrap, **applied once** when the database is created |
| `KEYCLOAK_ADMIN_PASSWORD` | The temporary Keycloak administrator | bootstrap, **used once** on Keycloak's first start |
| `REDIS_PASSWORD` | Redis | bootstrap |
| `MINIO_ROOT_PASSWORD` | Object storage (uploads, images, files, snapshots, archives) | bootstrap |
| `CLICKHOUSE_PASSWORD` | Langfuse's analytics store | bootstrap |
| `LANGFUSE_ENCRYPTION_KEY`, `LANGFUSE_SALT`, `LANGFUSE_NEXTAUTH_SECRET` | Langfuse's own encryption, hashing and sessions | bootstrap |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | The project key the platform traces with; Langfuse creates its project from them on first start | bootstrap |
| `LANGFUSE_INIT_USER_PASSWORD` | The first Langfuse user | bootstrap, used on first start |
| `TEMPORAL_UI_BASIC_AUTH_PASSWORD` | Temporal UI behind the proxy | bootstrap |
| `GATEWAY_API_KEY`, `OPENROUTER_API_KEY`, `IMAGES_API_KEY` | Your model provider accounts | you |
| `WEB_TOOLS_SERVICE_SECRET`, `SEARXNG_SECRET` | The web boundary (profile `web`) | you |
| `HERMES_EGRESS_SERVICE_SECRET` | `hermes-egress` asking the backend whether a tunnel is allowed | you |
| `HERMES_PREVIEW_SERVICE_SECRET`, `HERMES_PREVIEW_EXECUTOR_SECRET` | The preview service and the sandbox executor | you |
| `APP_ARCHIVE_*` S3 keys | The archive's own accounts ([data archive](data-archive.md)) | you |

Two defaults are fixed in `docker-compose.yml` and are not `.env` keys: the PostgreSQL
superuser (`postgres`) and MinIO's root user (`minio`). PostgreSQL's port is bound to
loopback only; do not publish it. Keycloak also uses the `postgres` account, and its
tables live in the `aurum_ai` database.

## Rotating

Rotate on a schedule you can keep and always after a suspected leak. Change the value
in `.env`, then recreate **every service that reads it** in one step, so that both
sides of a shared secret change together. A running `docker compose up -d` already
recreates services whose configuration changed.

| Secret | What else it takes | What users see |
|---|---|---|
| `APP_SECRET_KEY` | Recreate `backend`, `backend-admin` and, with the `maintenance` profile, `identity-worker` | Turns, Hermes sessions and previews that are open fail and must be started again; sign-in sessions (Keycloak) are not affected |
| `APP_DATABASE_ROLE_PASSWORD` | The role keeps its old password until you change it in the database: `ALTER ROLE aurum_ai_app PASSWORD '<new>'` as the PostgreSQL superuser, **then** recreate the APIs | Nothing, if done in that order. Editing `.env` alone makes the APIs unable to connect |
| `RAG_APP_DB_PASSWORD` | `ALTER ROLE` for the knowledge-base role in the same way, then recreate the RAG services | Knowledge search is unavailable between the two steps |
| `REDIS_PASSWORD` | Recreate Redis and every service with a Redis URL | Open turns may fail |
| `MINIO_ROOT_PASSWORD` | Recreate MinIO and its clients (APIs, `skill-executor`) | Uploads and downloads fail until the clients restart |
| `KEYCLOAK_ADMIN_PASSWORD` | Changing `.env` does nothing after the first start: change the administrator's password in the Keycloak console | None |
| `GATEWAY_API_KEY` and other provider keys | Recreate `backend` and `backend-admin` | None when the new key is valid |
| Service secrets (`WEB_TOOLS_*`, `HERMES_EGRESS_*`, `HERMES_PREVIEW_*`) | Recreate both sides together: the backend and the service that checks it | Web search or Hermes tunnels fail while the two sides disagree |
| `LANGFUSE_*` | Langfuse keeps what it created on first start. Rotate its project keys in Langfuse, put the new pair in `.env`, recreate the backend | Traces pause between the two steps |
| `TEMPORAL_UI_BASIC_AUTH_PASSWORD` | Recreate the proxy | The Temporal UI asks for the new password |

After any rotation: check `/ready` on both APIs, send one chat message, and for
Hermes restart `aurum-ai-hermes-egress-guard` and `aurum-ai-hermes-egress-1` if the
backend was recreated (the guard resolves the backend's address once when it starts).

Do not rotate `LANGFUSE_ENCRYPTION_KEY` or `LANGFUSE_SALT` on a Langfuse that holds
data you want to keep; they protect what Langfuse already stored.

## Handling

- Store `.env` in a secret manager or an encrypted volume, readable by the operator
  account only.
- Keep the development accounts (the realm's sample users) out of anything reachable
  by others; create real users in Keycloak.
- Never publish a complete environment dump, `docker compose config` output or a
  browser trace: they contain the values above and bearer tokens.
- A provider key belongs to the gateway or the backend, never to a browser, a runner
  or a Hermes sandbox: sandboxes receive only a session token of their own.
