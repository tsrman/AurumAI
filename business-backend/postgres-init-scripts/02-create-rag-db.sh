#!/bin/bash
# Replaces the old 02-create-rag-db.sql — a raw .sql file has no way to read
# RAG_APP_DB_PASSWORD from the environment (postgres's docker-entrypoint only
# interpolates env vars for .sh init scripts, not .sql ones), so the rag_app
# role's password was hardcoded in plaintext. Falls back
# to the same dev-only default as before when unset, matching every other
# secret in this compose stack (see .env.example) — see docker-compose.yml's
# `postgres` service for where RAG_APP_DB_PASSWORD is threaded through.
set -euo pipefail

RAG_APP_DB_PASSWORD="${RAG_APP_DB_PASSWORD:-rag_app}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE rag_app WITH LOGIN PASSWORD '${RAG_APP_DB_PASSWORD}';

    SELECT 'CREATE DATABASE rag_platform OWNER rag_app'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'rag_platform')\gexec

    GRANT ALL PRIVILEGES ON DATABASE rag_platform TO rag_app;
EOSQL
