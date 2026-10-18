import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from src.config import settings
from src.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

migration_url=settings.migration_database_url.get_secret_value()
if not migration_url:
    raise RuntimeError('rag_migration_database_url_required')
config.set_main_option("sqlalchemy.url", migration_url.replace("%", "%%"))

target_metadata = Base.metadata

# rag-api owns its own Postgres database (`rag_platform`, see
# postgres-init-scripts/02-create-rag-db.sql) — a plain default
# `alembic_version` table here can't collide with business-backend's,
# since they're not even in the same database.

def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()

def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()

async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = migration_url
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()

def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
