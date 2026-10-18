"""add app role and force rls

Revision ID: 2026_06_21_app_role_force_rls
Revises: b4f2b8c4b3f1
Create Date: 2026-06-21 12:00:00.000000

"""
import os
from typing import Sequence, Union

from alembic import op

from src.core.config import settings


revision: str = "2026_06_21_app_role_force_rls"
down_revision: Union[str, None] = "b4f2b8c4b3f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ROLE_NAME = "aurum_ai_app"
_DEFAULT_PASSWORD = "aurum_ai_app"
PASSWORD = os.environ.get("APP_DATABASE_ROLE_PASSWORD", _DEFAULT_PASSWORD)

# This role's password ends up in every backend/backend-admin container's
# APP_DATABASE_URL and has full DML on every tenant table — a fresh
# deployment that forgets to set APP_DATABASE_ROLE_PASSWORD would otherwise
# silently get a publicly-known password. Same
# DEBUG-only escape hatch as core/config.py's Settings fail-fast (H4);
# `settings.DEBUG` (not a raw os.environ read) so this also honors .env in
# local dev the same way env.py's own `settings.DATABASE_URL` use does.
if PASSWORD == _DEFAULT_PASSWORD and not settings.DEBUG:
    raise RuntimeError(
        "APP_DATABASE_ROLE_PASSWORD must be set outside DEBUG "
        "(generate with: openssl rand -hex 32)"
    )


def upgrade() -> None:
    safe_password = PASSWORD.replace("'", "''")
    op.execute(
        f"""
        DO $$
        BEGIN
            CREATE ROLE {ROLE_NAME} WITH LOGIN PASSWORD '{safe_password}';
        EXCEPTION WHEN duplicate_object THEN
            RAISE NOTICE 'Role "{ROLE_NAME}" already exists, skipping creation.';
        END
        $$
        """
    )
    op.execute(f"GRANT CONNECT ON DATABASE aurum_ai TO {ROLE_NAME}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE_NAME}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {ROLE_NAME}"
    )
    op.execute(
        f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ROLE_NAME}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ROLE_NAME}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {ROLE_NAME}"
    )

    tenant_tables = [
        "users",
        "conversations",
        "messages",
        "groups",
        "user_settings",
        "group_permissions",
        "user_groups",
        "skills",
    ]
    for table in tenant_tables:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    tenant_tables = [
        "skills",
        "user_groups",
        "group_permissions",
        "user_settings",
        "groups",
        "messages",
        "conversations",
        "users",
    ]
    for table in tenant_tables:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")

    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {ROLE_NAME}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM {ROLE_NAME}"
    )
    op.execute(
        f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {ROLE_NAME}"
    )
    op.execute(
        f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {ROLE_NAME}"
    )
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROLE_NAME}")
    op.execute(f"REVOKE CONNECT ON DATABASE aurum_ai FROM {ROLE_NAME}")
    op.execute(f"DROP OWNED BY {ROLE_NAME}")
    op.execute(f"DROP ROLE IF EXISTS {ROLE_NAME}")
