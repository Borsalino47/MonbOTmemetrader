"""Retention: does pruning ever delete the evidence it exists to protect?

Deleting rows is the one operation in this system that cannot be undone, and the
rows most likely to be deleted by a naive implementation — old signals — are
exactly the ones whose outcomes are about to arrive. So the tests here are
mostly about what pruning refuses to touch.
"""

from __future__ import annotations

import pytest

from cryptopulse.config.settings import DatabaseSettings
from cryptopulse.database import repo
from cryptopulse.database.models import (
    AlertRecord,
    ScanRunRecord,
    ScorePointRecord,
    SignalHorizonRecord,
    SignalRecord,
    _utcnow,
)
from cryptopulse.database.session import get_session, init_engine, reset_engine
from cryptopulse.outcomes.horizons import HORIZONS

DAY_MS = 86_400_000


@pytest.fixture()
def db(tmp_path):
    reset_engine()
    init_engine(DatabaseSettings(url=f"sqlite:///{tmp_path}/retention.db"))
    yield
    reset_engine()


def _now_ms() -> int:
    return int(_utcnow().timestamp() * 1000)


def _signal(session, *, age_days: float, graded: bool, horizons: int, symbol="SOLUSDT") -> int:
    """Insert one journal row `age_days` old, with the given completeness."""
    rec = SignalRecord(
        symbol=symbol,
        timestamp_ms=_now_ms() - int(age_days * DAY_MS),
        price=100.0, raw_score=60.0, risk_penalty=0.0, final_score=60.0,
        pump_maturity=20.0, data_confidence=90.0, safety_score=80.0,
        liquidity_status="GOOD", setup_state="ARMED",
        engine_version="SCORE_ENGINE_V1", weights_fingerprint="abc123",
        outcome_label="WIN" if graded else None,
    )
    session.add(rec)
    session.flush()
    for h in list(HORIZONS)[:horizons]:
        session.add(
            SignalHorizonRecord(
                signal_id=rec.id, symbol=symbol, horizon=h.name,
                status="RESOLVED", change_pct=1.0, net_change_pct=0.6, success=True,
            )
        )
    return rec.id


def _count(model) -> int:
    with get_session() as s:
        return s.query(model).count()


# --------------------------------------------------------------------------- #
# What pruning must never touch
# --------------------------------------------------------------------------- #


def test_an_old_signal_awaiting_its_verdict_is_never_pruned(db):
    """This is the whole point. A signal 200 days old with no outcome is not
    stale data — it is a row that is still owed an answer."""
    with get_session() as s:
        _signal(s, age_days=200, graded=False, horizons=len(HORIZONS))
        s.commit()

    removed = repo.prune(retention_days=90)
    assert removed["signals"] == 0
    assert _count(SignalRecord) == 1


def test_an_old_signal_missing_a_horizon_window_is_never_pruned(db):
    """Three of four windows recorded means the 24h answer is still coming."""
    with get_session() as s:
        _signal(s, age_days=200, graded=True, horizons=len(HORIZONS) - 1)
        s.commit()

    removed = repo.prune(retention_days=90)
    assert removed["signals"] == 0
    assert _count(SignalRecord) == 1


def test_a_signal_inside_the_retention_window_is_kept_even_when_complete(db):
    with get_session() as s:
        _signal(s, age_days=10, graded=True, horizons=len(HORIZONS))
        s.commit()

    repo.prune(retention_days=90)
    assert _count(SignalRecord) == 1


def test_retention_of_zero_disables_pruning_entirely(db):
    with get_session() as s:
        _signal(s, age_days=999, graded=True, horizons=len(HORIZONS))
        s.commit()

    out = repo.prune(retention_days=0)
    assert "skipped" in out
    assert _count(SignalRecord) == 1


# --------------------------------------------------------------------------- #
# What pruning does remove
# --------------------------------------------------------------------------- #


def test_a_fully_answered_signal_past_retention_is_removed_with_its_windows(db):
    with get_session() as s:
        _signal(s, age_days=200, graded=True, horizons=len(HORIZONS))
        s.commit()

    removed = repo.prune(retention_days=90)
    assert removed["signals"] == 1
    assert removed["signal_horizons"] == len(HORIZONS)
    assert _count(SignalRecord) == 0
    assert _count(SignalHorizonRecord) == 0, "orphan horizon rows would outlive their signal"


def test_score_points_are_cut_sooner_than_signals(db):
    """Operational noise is high volume and read only over a short window."""
    with get_session() as s:
        for age in (5, 40, 200):
            s.add(
                ScorePointRecord(
                    symbol="SOLUSDT", timestamp_ms=_now_ms() - age * DAY_MS,
                    final_score=50.0, raw_score=50.0, price=1.0, state="WATCH",
                )
            )
        s.commit()

    removed = repo.prune(retention_days=90)  # operational window = 22 days
    assert removed["score_points"] == 2, "40 and 200 days old go; 5 days stays"
    assert _count(ScorePointRecord) == 1


def test_scan_runs_are_cut_on_the_operational_window(db):
    with get_session() as s:
        for age in (1, 60):
            s.add(
                ScanRunRecord(
                    started_at_ms=_now_ms() - age * DAY_MS, duration_ms=100,
                    universe_size=10, scanned=10, succeeded=10, failed=0, provider="fixture",
                )
            )
        s.commit()

    repo.prune(retention_days=90)
    assert _count(ScanRunRecord) == 1


def test_alerts_are_cut_on_the_full_retention_window(db):
    """An alert is a record of what was surfaced, so it outlives a score point."""
    with get_session() as s:
        for age in (40, 200):
            s.add(
                AlertRecord(
                    symbol="SOLUSDT", level="HIGH", headline="x",
                    timestamp_ms=_now_ms() - age * DAY_MS, dedup_key="k",
                    price=1.0, final_score=80.0, pump_maturity=10.0,
                    data_confidence=90.0, safety=90.0, state="ARMED",
                )
            )
        s.commit()

    repo.prune(retention_days=90)
    assert _count(AlertRecord) == 1, "the 40-day alert survives the operational cut"


def test_pruning_is_idempotent(db):
    with get_session() as s:
        _signal(s, age_days=200, graded=True, horizons=len(HORIZONS))
        s.commit()

    first = repo.prune(retention_days=90)
    second = repo.prune(retention_days=90)
    assert first["signals"] == 1
    assert second["signals"] == 0


def test_pruning_reports_exactly_what_it_removed(db):
    with get_session() as s:
        _signal(s, age_days=200, graded=True, horizons=len(HORIZONS), symbol="AAAUSDT")
        _signal(s, age_days=200, graded=False, horizons=0, symbol="BBBUSDT")
        _signal(s, age_days=1, graded=True, horizons=len(HORIZONS), symbol="CCCUSDT")
        s.commit()

    removed = repo.prune(retention_days=90)
    assert removed["signals"] == 1
    assert removed["retention_days"] == 90
    assert removed["operational_days"] == 22
    assert _count(SignalRecord) == 2


# --------------------------------------------------------------------------- #
# Visibility
# --------------------------------------------------------------------------- #


def test_signals_held_back_are_countable(db):
    """A journal that stops shrinking must be explicable, not mysterious."""
    with get_session() as s:
        _signal(s, age_days=200, graded=False, horizons=0, symbol="AAAUSDT")
        _signal(s, age_days=200, graded=True, horizons=1, symbol="BBBUSDT")
        _signal(s, age_days=200, graded=True, horizons=len(HORIZONS), symbol="CCCUSDT")
        s.commit()

    assert repo.retained_but_unsettled() == 2


# --------------------------------------------------------------------------- #
# In-memory SQLite must behave like the file case
# --------------------------------------------------------------------------- #


async def test_an_in_memory_database_is_shared_with_the_worker_threads():
    """Found by running the service for real, not by a unit test.

    An in-memory SQLite database lives inside its connection, and the default
    pool hands out one connection per thread. Every write here goes through
    `asyncio.to_thread`, so without a shared pool the worker opens a second,
    empty database: the scan reports success, the tables are missing, and the
    journal is silently lost. That is the worst failure mode this project has —
    it looks exactly like a quiet market.
    """
    import asyncio

    from cryptopulse.config.settings import CryptoPulseSettings

    reset_engine()
    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.database.url = "sqlite:///:memory:"
    settings.scanner.min_quote_volume_24h = 100_000.0
    init_engine(settings.database)

    def write_from_another_thread() -> int:
        with get_session() as s:
            _signal(s, age_days=1, graded=False, horizons=0)
            s.commit()
        with get_session() as s:
            return s.query(SignalRecord).count()

    assert await asyncio.to_thread(write_from_another_thread) == 1
    # And the main thread sees the same database, not a private copy of it.
    assert _count(SignalRecord) == 1
    reset_engine()
