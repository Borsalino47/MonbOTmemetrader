"""Retention, migration and cost for the Robinhood half.

Three questions this file answers, all of them about the boring half of a
product that is meant to run unattended on a phone for months:

  * does pruning ever delete a decision that still owes an answer? (invariant 19)
  * do the new tables and columns land on a database that predates them? (§45)
  * does anything Robinhood run, or cost anything, before someone asks? (§41-42)
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from cryptopulse.config.settings import DatabaseSettings
from cryptopulse.database import repo
from cryptopulse.database.migrate import ensure_schema
from cryptopulse.database.models import Base
from cryptopulse.outcomes.robinhood_outcomes import RH_HORIZONS

ADDR = "0x" + "ab" * 20
DAY_MS = 86_400_000


@pytest.fixture()
def db(tmp_path):
    from cryptopulse.database.session import init_engine, reset_engine

    reset_engine()
    init_engine(DatabaseSettings(url=f"sqlite:///{tmp_path}/rh.db"))
    yield
    reset_engine()


def _signal(address: str, ts_ms: int, action: str = "BUY") -> dict:
    return {
        "contract_address": address,
        "symbol": "XYZ",
        "pool_address": "0xpool",
        "timestamp_ms": ts_ms,
        "price_usd": 1.0,
        "action": action,
        "engine_version": "ROBINHOOD_TRADE_DECISION_V1",
        "weights_fingerprint": "abc123",
    }


def _now_ms() -> int:
    from datetime import UTC, datetime

    return int(datetime.now(UTC).timestamp() * 1000)


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #


def test_an_old_decision_with_every_window_recorded_is_pruned(db):
    old = _now_ms() - 200 * DAY_MS
    repo.save_robinhood_signals([_signal(ADDR, old)])
    signal_id = repo.robinhood_signals_pending_horizons(_now_ms(), 10)[0]["id"]
    repo.save_robinhood_horizons([
        {
            "signal_id": signal_id, "contract_address": ADDR, "horizon": h.name,
            "status": "RESOLVED", "entry_price": 1.0, "price_at_horizon": 1.1,
            "change_pct": 10.0, "net_change_pct": 7.0, "success": True,
        }
        for h in RH_HORIZONS
    ])

    removed = repo.prune(90)
    assert removed["robinhood_signals"] == 1
    assert removed["robinhood_horizons"] == len(RH_HORIZONS)


def test_an_old_decision_still_owing_a_window_is_never_pruned(db):
    """Invariant 19. The rows still owing an answer are precisely the ones about
    to become evidence, and losing them would look like a quiet chain."""
    old = _now_ms() - 200 * DAY_MS
    repo.save_robinhood_signals([_signal(ADDR, old)])
    signal_id = repo.robinhood_signals_pending_horizons(_now_ms(), 10)[0]["id"]
    # Three of the four windows: still owed one.
    repo.save_robinhood_horizons([
        {
            "signal_id": signal_id, "contract_address": ADDR, "horizon": h.name,
            "status": "RESOLVED", "change_pct": 1.0, "net_change_pct": -2.0,
            "success": False,
        }
        for h in RH_HORIZONS[:-1]
    ])

    removed = repo.prune(90)
    assert removed["robinhood_signals"] == 0
    assert repo.robinhood_journal_counts()["decisions"] == 1


def test_a_recent_decision_is_never_pruned_however_complete(db):
    repo.save_robinhood_signals([_signal(ADDR, _now_ms())])
    signal_id = repo.robinhood_signals_pending_horizons(_now_ms() + 1, 10)[0]["id"]
    repo.save_robinhood_horizons([
        {
            "signal_id": signal_id, "contract_address": ADDR, "horizon": h.name,
            "status": "RESOLVED", "change_pct": 1.0, "net_change_pct": 1.0,
            "success": True,
        }
        for h in RH_HORIZONS
    ])
    assert repo.prune(90)["robinhood_signals"] == 0


def test_disabling_retention_touches_nothing(db):
    repo.save_robinhood_signals([_signal(ADDR, _now_ms() - 500 * DAY_MS)])
    assert "skipped" in repo.prune(0)
    assert repo.robinhood_journal_counts()["decisions"] == 1


# --------------------------------------------------------------------------- #
# The journal itself
# --------------------------------------------------------------------------- #


def test_a_duplicate_decision_is_skipped_never_updated(db):
    """A decision is a historical fact about a moment (invariant 13)."""
    row = _signal(ADDR, 1_700_000_000_000)
    assert repo.save_robinhood_signals([row]) == 1
    assert repo.save_robinhood_signals([row]) == 0
    assert repo.robinhood_journal_counts()["decisions"] == 1


def test_a_window_already_recorded_is_never_rewritten(db):
    repo.save_robinhood_signals([_signal(ADDR, 1_700_000_000_000)])
    signal_id = repo.robinhood_signals_pending_horizons(_now_ms(), 10)[0]["id"]
    window = {
        "signal_id": signal_id, "contract_address": ADDR, "horizon": "15m",
        "status": "RESOLVED", "change_pct": 5.0, "net_change_pct": 2.0, "success": True,
    }
    assert repo.save_robinhood_horizons([window]) == 1
    assert repo.save_robinhood_horizons([{**window, "change_pct": -50.0}]) == 0
    rows = repo.robinhood_horizon_rows()
    assert len(rows) == 1 and rows[0]["change_pct"] == 5.0


def test_the_journal_counts_decisions_by_action_without_a_rate(db):
    """Counts, never a rate: `stats` is the only place a rate is computed, and
    only ever with its `n`."""
    now = _now_ms()
    repo.save_robinhood_signals([
        _signal(ADDR, now, "BUY"),
        _signal("0x" + "cd" * 20, now, "AVOID"),
        _signal("0x" + "ef" * 20, now, "AVOID"),
    ])
    counts = repo.robinhood_journal_counts()
    assert counts["by_action"] == {"BUY": 1, "AVOID": 2}
    assert "rate" not in counts and "win_rate" not in counts


def test_the_address_is_stored_lowercase_so_a_token_has_one_identity(db):
    """A contract address is the only real identity a token has; two casings
    would journal one token as two."""
    now = _now_ms()
    repo.save_robinhood_signals([_signal(ADDR.upper(), now)])
    assert repo.save_robinhood_signals([_signal(ADDR.lower(), now)]) == 0


# --------------------------------------------------------------------------- #
# Migration (spec §45)
# --------------------------------------------------------------------------- #


def test_the_new_tables_and_columns_land_on_a_database_that_predates_them(tmp_path):
    """A user upgrading must not lose their journal, and must not have to
    delete anything to get the new screens."""
    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = sa.create_engine(url)

    # An "old" database: every table except the Robinhood ones, and `positions`
    # without the columns R9 added.
    old_tables = [
        t for name, t in Base.metadata.tables.items()
        if not name.startswith("robinhood_")
    ]
    Base.metadata.create_all(engine, tables=old_tables)
    with engine.begin() as conn:
        for column in ("entry_early", "entry_liquidity_usd", "entry_rug_risk", "chain_id"):
            conn.execute(sa.text(f"ALTER TABLE positions DROP COLUMN {column}"))

    inspector = sa.inspect(engine)
    assert "robinhood_signals" not in inspector.get_table_names()

    # The application's own startup path: create_all for new tables, then the
    # additive migration for new columns on existing ones.
    Base.metadata.create_all(engine)
    added = ensure_schema(engine)

    inspector = sa.inspect(engine)
    assert "robinhood_signals" in inspector.get_table_names()
    assert "robinhood_horizons" in inspector.get_table_names()
    position_columns = {c["name"] for c in inspector.get_columns("positions")}
    assert {"entry_early", "entry_liquidity_usd", "entry_rug_risk", "chain_id"} <= position_columns
    assert any("positions.entry_" in a for a in added)
    engine.dispose()


def test_the_migration_refuses_to_destroy_anything():
    """Additive only. A destructive migration needs a human and a backup, not
    an automatic best effort."""
    import re
    from pathlib import Path

    source = Path("cryptopulse/database/migrate.py").read_text(encoding="utf-8")
    # Only the DDL it actually executes — the module's own prose says it will
    # not drop or rename anything, and matching on that would flag the promise
    # rather than the code.
    statements = re.findall(r'''f?['"](ALTER TABLE[^'"]*)['"]''', source)
    assert statements, "aucune instruction DDL trouvée"
    for statement in statements:
        upper = statement.upper()
        assert "ADD COLUMN" in upper, statement
        for forbidden in ("DROP", "RENAME", "ALTER COLUMN", "MODIFY"):
            assert forbidden not in upper, statement


# --------------------------------------------------------------------------- #
# Cost, and what runs before someone asks (spec §41-42)
# --------------------------------------------------------------------------- #


def test_nothing_robinhood_is_constructed_at_import_time():
    """Lazy by construction: a user who never opens the Robinhood tab must not
    pay for it, and the screen must never wait for a chain check."""
    from pathlib import Path

    source = Path("cryptopulse/api/service.py").read_text(encoding="utf-8")
    for attribute in ("_robinhood_watcher", "_robinhood_tracker"):
        assert f"self.{attribute} = None" in source, attribute
    # Built on first use, never in __init__.
    assert "def _get_robinhood_watcher" in source
    assert "def _get_robinhood_tracker" in source


def test_every_robinhood_loop_states_what_it_spent():
    """A loop that quietly spent hundreds of requests would be discovered as a
    rate-limit ban rather than as a number on a screen (invariant 28)."""
    from pathlib import Path

    for path, field in (
        ("cryptopulse/trading/robinhood_watcher.py", "requests_used"),
        ("cryptopulse/outcomes/robinhood_outcomes.py", "requests_used"),
        ("cryptopulse/hunter/robinhood_discovery.py", "safety_requests"),
    ):
        source = Path(path).read_text(encoding="utf-8")
        assert field in source, f"{path} ne dit pas ce qu'il a dépensé"
