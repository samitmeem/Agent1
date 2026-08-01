"""Alembic environment for the Ticket Assistant database.

Uses async SQLAlchemy with asyncpg so migrations run against the same driver
the application uses at runtime. The DATABASE_URL is read from the environment
(falling back to the value in alembic.ini).
"""
from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import the metadata so autogenerate has a target.  The models are defined in
# the initial migration itself; for offline/online upgrades we only need the
# ``target_metadata`` to be non-None so Alembic's ``--sql`` mode works.
from sqlalchemy import MetaData

target_metadata = MetaData()

config = context.config

# Honour DATABASE_URL from the environment (compose / local dev) when present.
if os.environ.get("DATABASE_URL"):
    # Alembic expects a SQLAlchemy URL; asyncpg uses the +asyncpg dialect.
    db_url = os.environ["DATABASE_URL"]
    if not db_url.startswith("postgresql+asyncpg"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode with an async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
