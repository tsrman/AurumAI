#!/usr/bin/env bash
# Create persistent local secrets; preserve existing configured values.
set -euo pipefail
umask 077

aurum_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
aurum_target="$aurum_root/.env"
if [[ $# -gt 0 ]]; then
  [[ $# -eq 2 && "$1" == --env-file ]] || { echo "Usage: bootstrap-dev-env.sh [--env-file PATH]" >&2; exit 1; }
  aurum_target="$2"
fi
[[ ! -L "$aurum_target" && ( ! -e "$aurum_target" || -f "$aurum_target" ) ]] || {
  echo "Environment target must be a regular file." >&2; exit 1;
}
command -v openssl >/dev/null || { echo "OpenSSL is required." >&2; exit 1; }
aurum_lock="${aurum_target}.bootstrap.lock"
mkdir "$aurum_lock" 2>/dev/null || { echo "Environment bootstrap is already locked; preserve and inspect the lock." >&2; exit 1; }
aurum_temporary=""
cleanup() {
  [[ -z "$aurum_temporary" ]] || rm -f "$aurum_temporary"
  rmdir "$aurum_lock"
}
trap cleanup EXIT
trap 'exit 1' INT TERM HUP
aurum_temporary="$(mktemp "${aurum_target}.tmp.XXXXXX")"
if [[ -f "$aurum_target" ]]; then
  cat "$aurum_target" > "$aurum_temporary"
else
  cat "$aurum_root/.env.example" > "$aurum_temporary"
fi

aurum_keys="APP_SECRET_KEY APP_DATABASE_ROLE_PASSWORD KEYCLOAK_ADMIN_PASSWORD TEMPORAL_UI_BASIC_AUTH_PASSWORD RAG_APP_DB_PASSWORD REDIS_PASSWORD MINIO_ROOT_PASSWORD CLICKHOUSE_PASSWORD LANGFUSE_ENCRYPTION_KEY LANGFUSE_NEXTAUTH_SECRET LANGFUSE_SALT LANGFUSE_INIT_USER_PASSWORD LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY"
aurum_parser="$aurum_root/scripts/bootstrap-dev-env.awk"
awk -f "$aurum_parser" -v action=validate -v keys="$aurum_keys" "$aurum_temporary"
for aurum_key in $aurum_keys; do
  aurum_state="$(awk -f "$aurum_parser" -v action=state -v key="$aurum_key" "$aurum_temporary")"
  if [[ "$aurum_state" == configured ]]; then
    continue
  fi
  aurum_new="${!aurum_key:-}"
  if [[ -z "$aurum_new" ]]; then
    aurum_new="$(openssl rand -hex 32)"
    case "$aurum_key" in
      LANGFUSE_PUBLIC_KEY) aurum_new="pk-lf-$aurum_new" ;;
      LANGFUSE_SECRET_KEY) aurum_new="sk-lf-$aurum_new" ;;
    esac
  fi
  [[ "$aurum_new" != *$'\n'* && "$aurum_new" != *$'\r'* ]] || { echo "Secret overrides must be single-line values." >&2; exit 1; }
  aurum_next="$(mktemp "${aurum_target}.tmp.XXXXXX")"
  if ! AURUMAI_GENERATED_SECRET="$aurum_new" awk -f "$aurum_parser" \
    -v action=replace -v key="$aurum_key" "$aurum_temporary" > "$aurum_next"; then
    rm -f "$aurum_next"
    exit 1
  fi
  mv "$aurum_next" "$aurum_temporary"
done
chmod 600 "$aurum_temporary"
mv "$aurum_temporary" "$aurum_target"
aurum_temporary=""
echo "AurumAI development environment is ready."
