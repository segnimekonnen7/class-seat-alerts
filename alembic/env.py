"""
Alembic environment.

Two things worth noting:

  1. The database URL comes from the app's settings, not alembic.ini, so a
     credential never has to live in a checked-in file.
  2. Every model is imported below so Base.metadata is complete before
     autogenerate compares it to the database. A model that is not imported is
     invisible to `alembic check`, and CI would pass happily while the schema
     drifted -- which defeats the point of running the check.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.config import get_settings
from app.db import Base
from app.models import (  # noqa: F401 - imported for their effect on Base.metadata
    Notification,
    Section,
    StatusEvent,
    Watch,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it against a live database."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # compare_type catches a column that changed from Integer to String;
            # without it `alembic check` would call that no drift.
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
