"""Additive schema migration.

`Base.metadata.create_all` creates missing *tables* but never alters existing
ones, so a database created before a column was added silently lacks it and every
query against that column fails at runtime.

This module closes that gap for the only kind of change this project makes so
far: adding a nullable column. It compares the live table to the ORM model and
issues `ALTER TABLE ... ADD COLUMN` for anything missing.

Deliberately limited. It will not drop, rename or retype a column, and it says so
rather than guessing — destructive migrations need a human and a backup, not an
automatic best effort. If a real migration tool becomes necessary, switch to
Alembic; this exists so the outcome columns can land without anyone losing their
accumulated signal journal.
"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from cryptopulse.core.logging import get_logger
from cryptopulse.database.models import Base

log = get_logger("database.migrate")

__all__ = ["ensure_schema"]


def _sql_type(engine: Engine, column) -> str:
    return column.type.compile(dialect=engine.dialect)


def ensure_schema(engine: Engine) -> list[str]:
    """Add any column present in the models but missing from the database.

    Returns the list of `table.column` names that were added.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []

    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            continue  # create_all handles brand-new tables

        live_columns = {c["name"] for c in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name in live_columns:
                continue

            if not column.nullable and column.default is None and column.server_default is None:
                # Adding a NOT NULL column to a populated table cannot succeed
                # without a value for the existing rows. Refuse loudly.
                log.error(
                    "migration_needs_manual_step",
                    table=table_name,
                    column=column.name,
                    reason="new column is NOT NULL with no default; existing rows have no value for it",
                )
                continue

            ddl = f'ALTER TABLE {table_name} ADD COLUMN {column.name} {_sql_type(engine, column)}'
            with engine.begin() as conn:
                conn.execute(text(ddl))
            added.append(f"{table_name}.{column.name}")
            log.info("column_added", table=table_name, column=column.name)

    if added:
        log.info("schema_migrated", added=added)
    return added
