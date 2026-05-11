"""Alembic environment configuration.

Reads DATABASE_URL from the environment and normalises it for psycopg2 /
SQLAlchemy (which Alembic uses for migrations).

asyncpg accepts plain "postgresql://" URLs.  SQLAlchemy's psycopg2 driver
also accepts "postgresql://" but NOT "postgresql+asyncpg://" — so we strip
any driver qualifier and let SQLAlchemy default to psycopg2.

Supported input formats
-----------------------
* postgresql://user:pass@host:5432/db          — no change needed
* postgresql+asyncpg://user:pass@host:5432/db  — strip "+asyncpg"
* postgres://user:pass@host:5432/db            — Heroku shorthand; rewrite scheme
"""

import os
import re
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------


def _to_psycopg2_url(url: str) -> str:
    """Convert any PostgreSQL URL variant to one psycopg2 / SQLAlchemy accepts.

    SQLAlchemy's default PostgreSQL dialect is psycopg2, which accepts
    ``postgresql://`` but not ``postgresql+asyncpg://`` or ``postgres://``.
    """
    # Strip driver qualifier (e.g. "+asyncpg", "+psycopg2") if present
    url = re.sub(r"postgresql\+\w+://", "postgresql://", url)
    # Heroku / some cloud providers use "postgres://" (no "ql")
    url = re.sub(r"^postgres://", "postgresql://", url)
    return url


raw_url = os.environ.get("DATABASE_URL", "")
if not raw_url:
    raise RuntimeError(
        "DATABASE_URL environment variable is not set. "
        "Set it before running alembic commands."
    )

db_url = _to_psycopg2_url(raw_url)

# Override the INI-file URL with the one we just built
config.set_main_option("sqlalchemy.url", db_url)

# target_metadata is None because we write raw SQL in migrations, not ORM models
target_metadata = None


# ---------------------------------------------------------------------------
# Migration runners
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout / file).

    Useful for generating a SQL script to review before applying.
    """
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (apply directly against the DB)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # NullPool: no persistent connections during migrations
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
