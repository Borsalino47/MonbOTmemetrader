"""PostgreSQL compatibility.

The bug this file exists to prevent was live for the whole project: every
millisecond timestamp was declared `Integer`. SQLite stores integers dynamically
and never complained, so the schema looked fine for as long as nobody deployed
it — and on PostgreSQL, whose INTEGER stops at 2.1e9 against a timestamp of
1.8e12, *every insert would have failed*. "Untested" was hiding "broken".

Two layers of defence:

* the dialect tests below need no server and run everywhere, catching the type
  the moment it regresses;
* the integration test runs the real pipeline against a real server when
  `CP_TEST_POSTGRES_URL` points at one, and skips otherwise.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from cryptopulse.config.settings import DatabaseSettings
from cryptopulse.database.models import Base

POSTGRES_URL = os.environ.get("CP_TEST_POSTGRES_URL")

# Any column holding a millisecond epoch. 32 bits is not enough for any of them.
MS_COLUMNS = [
    (table_name, column.name)
    for table_name, table in Base.metadata.tables.items()
    for column in table.columns
    if column.name.endswith("_ms") and column.name != "duration_ms"
]


def test_the_model_declares_every_millisecond_column_wide_enough():
    assert MS_COLUMNS, "no millisecond columns found — the model must have moved"
    for table_name, column_name in MS_COLUMNS:
        column = Base.metadata.tables[table_name].columns[column_name]
        assert isinstance(column.type, BigInteger), (
            f"{table_name}.{column_name} is {column.type!r}. A millisecond epoch is ~1.8e12 and "
            "PostgreSQL's INTEGER stops at 2.1e9, so this column cannot hold a timestamp."
        )


@pytest.mark.parametrize(("table_name", "column_name"), MS_COLUMNS)
def test_postgres_ddl_emits_bigint(table_name, column_name):
    """Check the SQL actually sent to PostgreSQL, not just the Python type."""
    ddl = str(CreateTable(Base.metadata.tables[table_name]).compile(dialect=postgresql.dialect()))
    line = next(ln.strip() for ln in ddl.splitlines() if ln.strip().startswith(f"{column_name} "))
    assert "BIGINT" in line, f"{table_name}.{column_name} compiles to: {line}"


def test_a_millisecond_timestamp_exceeds_a_32_bit_column():
    """The arithmetic the bug turned on, stated once so the reason survives."""
    a_real_timestamp_ms = 1_788_159_000_000
    assert a_real_timestamp_ms > 2**31 - 1


@pytest.mark.skipif(not POSTGRES_URL, reason="set CP_TEST_POSTGRES_URL to run against a real server")
def test_the_whole_journal_round_trips_on_a_real_postgres(tmp_path):
    """Create the schema, write a signal with a real timestamp, read it back."""
    import asyncio

    from cryptopulse.config.settings import CryptoPulseSettings
    from cryptopulse.core.clock import FrozenClock
    from cryptopulse.database import repo
    from cryptopulse.database.session import init_engine, reset_engine
    from cryptopulse.providers.fixture import FixtureProvider
    from cryptopulse.scanner.cex import CexScanner
    from tests.conftest import FIXED_NOW_MS

    engine = create_engine(POSTGRES_URL)
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.exec_driver_sql(f"DROP TABLE IF EXISTS {table.name} CASCADE")
    engine.dispose()

    reset_engine()
    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.scanner.universe = "robinhood"
    settings.database.url = POSTGRES_URL
    init_engine(settings.database)

    async def run():
        clock = FrozenClock(FIXED_NOW_MS)
        scanner = CexScanner(settings, provider=FixtureProvider(clock=clock), clock=clock)
        report = await scanner.scan()
        written = repo.persist_scan(report, provider="fixture", regime="RANGE")
        await scanner.close()
        return written

    written = asyncio.run(run())
    assert written > 0, "nothing was written — the schema rejected the rows"

    rows = repo.recent_signals(limit=5)
    assert rows
    assert rows[0]["timestamp_ms"] > 2**31 - 1, "the timestamp survived the round trip intact"
    reset_engine()


def test_the_url_is_redacted_before_it_reaches_a_log():
    """A Postgres URL carries a password; the log line must not."""
    settings = DatabaseSettings(url="postgresql+psycopg://user:hunter2@db.internal:5432/cryptopulse")
    from cryptopulse.database.session import _redact

    safe = _redact(settings.url)
    assert "hunter2" not in safe
    assert "db.internal" in safe
