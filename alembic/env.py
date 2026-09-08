"""Alembic environment.

Two things make this file different from the stock template:

* the database URL comes from ``app.core.config`` (i.e. from ``.env``) rather
  than from ``alembic.ini``, so credentials live in exactly one place;
* ``target_metadata`` points at our ``Base.metadata``, which is what lets
  ``alembic revision --autogenerate`` diff the models against the live schema.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import settings

# Importing the models module registers User and Trade on Base.metadata.
# Without this import autogenerate would see an empty schema.
from app.db import models  # noqa: F401
from app.db.database import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (``alembic upgrade --sql``)."""
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Notice a column whose type changed, not just added/dropped ones.
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Connect to the database and run the migrations.

    The application uses an async driver, so Alembic gets an async engine too.
    ``run_sync`` bridges the gap: Alembic's migration machinery is synchronous,
    and this runs it on the greenlet backing the async connection.
    """
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
