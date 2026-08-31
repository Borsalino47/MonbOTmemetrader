"""Repository: the only module that writes to the database.

The signal journal is what turns this from a scanner into something that can be
evaluated. Every scored asset above OBSERVE is written with its full breakdown so
that, after enough occurrences, it becomes possible to ask which components
actually carried predictive value — rather than assuming the V1 weights were right.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sqlalchemy import delete, desc, func, select
from sqlalchemy.exc import IntegrityError

from cryptopulse.alerts.engine import Alert
from cryptopulse.core.logging import get_logger
from cryptopulse.database.models import AlertRecord, ScanRunRecord, ScorePointRecord, SignalRecord, _utcnow
from cryptopulse.database.session import get_session
from cryptopulse.scanner.base import ScanReport
from cryptopulse.scoring.states import SetupState

if TYPE_CHECKING:  # avoids a circular import at runtime: outcomes -> backtest -> ...
    from cryptopulse.outcomes.tracker import PendingSignal, Resolution

log = get_logger("database.repo")

__all__ = [
    "persist_scan", "persist_alerts", "recent_signals", "score_history", "recent_alerts", "signal_stats",
    "pending_signals", "save_resolutions", "resolved_signals", "outcome_counts",
    "pending_moonshot_signals", "save_moonshot_resolutions", "resolved_moonshot_signals",
    "moonshot_counts",
]

_PERSIST_STATES = {
    SetupState.OBSERVE,
    SetupState.WATCH,
    SetupState.ARMED,
    SetupState.BREAKOUT,
    SetupState.RETEST,
    SetupState.CONTINUATION,
}

# Stages whose reading is a claim worth grading later. NEUTRAL and UNKNOWN are
# not claims, so journalling them would only dilute the statistics.
_PERSIST_MOONSHOT_STAGES = {"ACCUMULATION", "IGNITION", "EXPANSION", "DORMANT"}

# Below this the moonshot reading is not asserting anything either.
DEFAULT_MOONSHOT_JOURNAL_MIN_SCORE = 50.0


def _should_persist(r, moonshot_min_score: float) -> bool:
    """Whether this scored asset belongs in the journal.

    The setup state alone is not enough any more. The assets the ×10 layer exists
    to find are precisely the ones with no intraday setup — a dormant base scores
    IGNORE on the setup axis — so filtering on that state alone guarantees the
    moonshot journal stays empty and the layer stays unvalidated forever.
    """
    if r.state.state in _PERSIST_STATES:
        return True
    m = getattr(r, "moonshot", None)
    return (
        m is not None
        and m.stage.value in _PERSIST_MOONSHOT_STAGES
        and m.score >= moonshot_min_score
    )


def _signal_record(r, *, provider: str, regime: str | None, synthetic: bool) -> SignalRecord:
    """Build one journal row.

    Single builder on purpose: the batch path and the row-by-row fallback below
    used to construct this twice, which is exactly how a column ends up written
    on one path and silently NULL on the other.
    """
    st = r.features.primary.structure if r.features else None
    m = getattr(r, "moonshot", None)
    valuation = r.features.valuation if (r.features and r.features.valuation) else None
    return SignalRecord(
        symbol=r.symbol,
        timestamp_ms=r.timestamp_ms,
        price=r.price,
        raw_score=r.raw_score,
        risk_penalty=r.risk_penalty,
        final_score=r.final_score,
        score_acceleration=r.score_acceleration,
        pump_maturity=r.maturity.score,
        data_confidence=r.confidence.score,
        safety_score=r.safety.score,
        liquidity_status=r.liquidity.status.value,
        setup_state=r.state.state.value,
        is_premium=r.is_premium,
        hard_veto=r.safety.hard_veto or r.liquidity.veto,
        engine_version=r.engine_version,
        weights_fingerprint=r.weights_fingerprint,
        data_source=provider,
        synthetic=synthetic,
        market_regime=regime,
        atr=r.features.primary.atr14 if r.features else None,
        rvol=r.features.primary.rvol if r.features else None,
        breakout_level=st.nearest_resistance.price if (st and st.nearest_resistance) else None,
        distance_to_breakout_atr=st.distance_to_resistance_atr if st else None,
        components={c.name: round(c.points, 2) for c in r.components},
        penalties=r.penalties.to_dict(),
        why=r.why()[:12],
        risks=r.risks()[:12],
        # The ×10 axis. NULL when the layer produced no reading — never zero.
        moonshot_score=m.score if m else None,
        moonshot_stage=m.stage.value if m else None,
        moonshot_ignition=m.ignition if m else None,
        moonshot_headroom=m.headroom if m else None,
        moonshot_capacity=m.capacity if m else None,
        moonshot_coverage=m.coverage if m else None,
        moonshot_timeframe=m.timeframe if m else None,
        moonshot_multiple_to_high=m.multiple_to_window_high if m else None,
        moonshot_engine_version=m.engine_version if m else None,
        market_cap_usd=valuation.market_cap_usd if valuation else None,
    )


def persist_scan(
    report: ScanReport,
    *,
    provider: str,
    regime: str | None = None,
    moonshot_journal_min_score: float = DEFAULT_MOONSHOT_JOURNAL_MIN_SCORE,
) -> int:
    """Write the scan run, the score points and the qualifying signals.

    Returns the number of signal rows written. Duplicate (symbol, timestamp,
    engine) rows are skipped rather than updated: a signal is a historical fact
    about a moment, and rewriting it would corrupt the journal.
    """
    written = 0
    with get_session() as session:
        session.add(
            ScanRunRecord(
                started_at_ms=report.started_at_ms,
                duration_ms=report.duration_ms,
                universe_size=report.universe_size,
                scanned=report.scanned,
                succeeded=report.succeeded,
                failed=report.failed,
                provider=provider,
                synthetic=report.synthetic_data,
                errors=json.dumps(report.errors)[:8000] if report.errors else None,
            )
        )

        for r in report.results:
            session.add(
                ScorePointRecord(
                    symbol=r.symbol,
                    timestamp_ms=r.timestamp_ms,
                    final_score=r.final_score,
                    raw_score=r.raw_score,
                    price=r.price,
                    state=r.state.state.value,
                )
            )
            if _should_persist(r, moonshot_journal_min_score):
                session.add(
                    _signal_record(r, provider=provider, regime=regime, synthetic=report.synthetic_data)
                )
                written += 1

        try:
            session.commit()
        except IntegrityError:
            # A re-scan inside the same candle produces the same (symbol, ts).
            session.rollback()
            written = _commit_individually(report, provider, regime, moonshot_journal_min_score)

    return written


def _commit_individually(
    report: ScanReport, provider: str, regime: str | None, moonshot_journal_min_score: float
) -> int:
    """Fallback after a batch conflict: insert row by row, skipping duplicates."""
    written = 0
    for r in report.results:
        if not _should_persist(r, moonshot_journal_min_score):
            continue
        with get_session() as session:
            exists = session.execute(
                select(SignalRecord.id).where(
                    SignalRecord.symbol == r.symbol,
                    SignalRecord.timestamp_ms == r.timestamp_ms,
                    SignalRecord.engine_version == r.engine_version,
                )
            ).first()
            if exists:
                continue
            session.add(_signal_record(r, provider=provider, regime=regime, synthetic=report.synthetic_data))
            try:
                session.commit()
                written += 1
            except IntegrityError:
                session.rollback()
    return written


def persist_alerts(alerts: list[Alert]) -> int:
    if not alerts:
        return 0
    with get_session() as session:
        for a in alerts:
            session.add(
                AlertRecord(
                    symbol=a.symbol,
                    level=a.level.value,
                    headline=a.headline,
                    timestamp_ms=a.timestamp_ms,
                    dedup_key=a.dedup_key,
                    price=a.price,
                    final_score=a.final_score,
                    pump_maturity=a.pump_maturity,
                    data_confidence=a.data_confidence,
                    safety=a.safety,
                    state=a.state,
                    payload=a.to_dict(),
                )
            )
        session.commit()
    return len(alerts)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def recent_signals(limit: int = 100, symbol: str | None = None, min_score: float | None = None) -> list[dict]:
    with get_session() as session:
        stmt = select(SignalRecord).order_by(desc(SignalRecord.timestamp_ms)).limit(limit)
        if symbol:
            stmt = stmt.where(SignalRecord.symbol == symbol.upper())
        if min_score is not None:
            stmt = stmt.where(SignalRecord.final_score >= min_score)
        rows = session.execute(stmt).scalars().all()
        return [_signal_to_dict(r) for r in rows]


def score_history(symbol: str, limit: int = 300) -> list[dict]:
    with get_session() as session:
        rows = (
            session.execute(
                select(ScorePointRecord)
                .where(ScorePointRecord.symbol == symbol.upper())
                .order_by(desc(ScorePointRecord.timestamp_ms))
                .limit(limit)
            )
            .scalars()
            .all()
        )
    return [
        {
            "timestamp_ms": r.timestamp_ms,
            "final_score": round(r.final_score, 2),
            "raw_score": round(r.raw_score, 2),
            "price": r.price,
            "state": r.state,
        }
        for r in reversed(rows)
    ]


def recent_alerts(limit: int = 50) -> list[dict]:
    with get_session() as session:
        rows = (
            session.execute(select(AlertRecord).order_by(desc(AlertRecord.timestamp_ms)).limit(limit)).scalars().all()
        )
    return [r.payload for r in rows]


def signal_stats() -> dict:
    """Headline counts for the signals view.

    The win rate stays `None` until enough signals carry a settled verdict.
    UNRESOLVABLE rows never enter the denominator — they have no verdict, so
    counting them either invents a loss or dilutes the rate.
    """
    from cryptopulse.outcomes.stats import MIN_SAMPLE

    counts = outcome_counts()
    settled = counts["settled"]
    wins = counts["wins"]

    if settled == 0:
        note = (
            "No outcome has been resolved yet. Win rate is null until the outcome tracker "
            "has graded signals against the bars that followed them."
        )
    elif settled < MIN_SAMPLE:
        note = (
            f"Based on only {settled} settled signals — below the {MIN_SAMPLE} minimum. "
            "Treat this as a smoke test of the pipeline, not as evidence about the strategy."
        )
    else:
        note = f"Based on {settled} settled signals (WIN/LOSS/TIMEOUT)."

    if counts["synthetic_signals"]:
        note += (
            f" {counts['synthetic_signals']} signal(s) came from the SYNTHETIC provider "
            "and describe generated data, not a market."
        )

    return {
        **counts,
        "win_rate": round(wins / settled, 4) if settled else None,
        "win_rate_note": note,
        "min_sample": MIN_SAMPLE,
        "sufficient_sample": settled >= MIN_SAMPLE,
    }


def purge_older_than(cutoff_ms: int) -> int:
    with get_session() as session:
        result = session.execute(delete(ScorePointRecord).where(ScorePointRecord.timestamp_ms < cutoff_ms))
        session.commit()
        return result.rowcount or 0


def _signal_to_dict(r: SignalRecord) -> dict:
    return {
        "id": r.id,
        "symbol": r.symbol,
        "timestamp_ms": r.timestamp_ms,
        "price": r.price,
        "raw_score": round(r.raw_score, 2),
        "risk_penalty": round(r.risk_penalty, 2),
        "final_score": round(r.final_score, 2),
        "score_acceleration": r.score_acceleration,
        "pump_maturity": round(r.pump_maturity, 1),
        "data_confidence": round(r.data_confidence, 1),
        "safety": round(r.safety_score, 1),
        "liquidity": r.liquidity_status,
        "state": r.setup_state,
        "is_premium": r.is_premium,
        "engine_version": r.engine_version,
        "data_source": r.data_source,
        "synthetic": r.synthetic,
        "market_regime": r.market_regime,
        "breakout_level": r.breakout_level,
        "distance_to_breakout_atr": r.distance_to_breakout_atr,
        "components": r.components,
        "why": r.why,
        "risks": r.risks,
        "outcome": {
            "label": r.outcome_label,
            "return_pct": r.outcome_return_pct,
            "mfe_atr": r.outcome_mfe_atr,
            "mae_atr": r.outcome_mae_atr,
            "evaluated": r.outcome_label is not None,
        },
    }


# --------------------------------------------------------------------------- #
# Outcome tracking
# --------------------------------------------------------------------------- #


def pending_signals(ready_before_ms: int, limit: int = 300) -> list[PendingSignal]:
    """Signals with no outcome yet whose horizon has had time to elapse.

    Oldest first: the ones closest to falling out of the provider's reachable
    history are the ones most at risk of becoming permanently unresolvable.
    """
    from cryptopulse.config.settings import get_settings
    from cryptopulse.core.types import Timeframe
    from cryptopulse.outcomes.tracker import PendingSignal

    tf = get_settings().scanner.primary_timeframe
    with get_session() as session:
        rows = (
            session.execute(
                select(SignalRecord)
                .where(SignalRecord.outcome_label.is_(None))
                .where(SignalRecord.timestamp_ms <= ready_before_ms)
                .order_by(SignalRecord.timestamp_ms.asc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
    return [
        PendingSignal(
            id=r.id,
            symbol=r.symbol,
            timestamp_ms=r.timestamp_ms,
            price=r.price,
            atr=r.atr,
            timeframe=Timeframe(tf) if not isinstance(tf, Timeframe) else tf,
        )
        for r in rows
    ]


def save_resolutions(resolutions: list[Resolution]) -> int:
    """Write graded outcomes back onto their signal rows."""
    if not resolutions:
        return 0
    written = 0
    with get_session() as session:
        for res in resolutions:
            row = session.get(SignalRecord, res.signal_id)
            if row is None:
                continue
            # Never overwrite a verdict. A signal is graded once; re-grading it
            # under a different label config would silently rewrite history.
            if row.outcome_label is not None:
                continue
            row.outcome_label = res.label
            row.outcome_label_config = res.label_config
            row.outcome_horizon_bars = res.horizon_bars
            row.outcome_return_pct = res.return_pct
            row.outcome_net_return_pct = res.net_return_pct
            row.outcome_mfe_atr = res.mfe_atr
            row.outcome_mae_atr = res.mae_atr
            row.outcome_bars_held = res.bars_held
            row.outcome_entry_price = res.entry_price
            row.outcome_exit_price = res.exit_price
            row.outcome_note = res.note
            row.outcome_evaluated_at = _utcnow()
            written += 1
        session.commit()
    return written


def resolved_signals(limit: int | None = None, include_synthetic: bool = True) -> list[dict]:
    """Every signal carrying a settled verdict (WIN / LOSS / TIMEOUT).

    UNRESOLVABLE rows are excluded on purpose: they have no verdict, and folding
    them into a rate would either invent a loss or dilute the denominator.
    """
    with get_session() as session:
        stmt = (
            select(SignalRecord)
            .where(SignalRecord.outcome_label.in_(["WIN", "LOSS", "TIMEOUT"]))
            .order_by(desc(SignalRecord.timestamp_ms))
        )
        if not include_synthetic:
            stmt = stmt.where(SignalRecord.synthetic.is_(False))
        if limit:
            stmt = stmt.limit(limit)
        rows = session.execute(stmt).scalars().all()
    return [_outcome_row(r) for r in rows]


def _outcome_row(r: SignalRecord) -> dict:
    return {
        "id": r.id,
        "symbol": r.symbol,
        "timestamp_ms": r.timestamp_ms,
        "price": r.price,
        "final_score": r.final_score,
        "raw_score": r.raw_score,
        "risk_penalty": r.risk_penalty,
        "pump_maturity": r.pump_maturity,
        "data_confidence": r.data_confidence,
        "safety": r.safety_score,
        "liquidity": r.liquidity_status,
        "state": r.setup_state,
        "is_premium": r.is_premium,
        "regime": r.market_regime,
        "engine_version": r.engine_version,
        "synthetic": r.synthetic,
        "components": r.components or {},
        "outcome": r.outcome_label,
        "label_config": r.outcome_label_config,
        "return_pct": r.outcome_return_pct,
        "net_return_pct": r.outcome_net_return_pct,
        "mfe_atr": r.outcome_mfe_atr,
        "mae_atr": r.outcome_mae_atr,
        "bars_held": r.outcome_bars_held,
    }


# --------------------------------------------------------------------------- #
# The ×10 axis — its own verdict, on its own clock
#
# A signal carries two independent theses: what happens in the next few hours,
# and what happens in the next few weeks. They are graded separately, into
# separate columns, because sharing one verdict would force a choice between
# answering the first question and answering the second.
# --------------------------------------------------------------------------- #


def pending_moonshot_signals(ready_before_ms: int, limit: int = 300) -> list[PendingSignal]:
    """Signals carrying a ×10 reading that has not been graded yet.

    Only rows with a `moonshot_score`: grading a row the layer never assessed
    would put an unrelated asset into the ×10 statistics.
    """
    from cryptopulse.config.settings import get_settings
    from cryptopulse.core.types import Timeframe
    from cryptopulse.outcomes.tracker import PendingSignal

    tf = get_settings().scanner.primary_timeframe
    with get_session() as session:
        rows = (
            session.execute(
                select(SignalRecord)
                .where(SignalRecord.moon_outcome_label.is_(None))
                .where(SignalRecord.moonshot_score.is_not(None))
                .where(SignalRecord.timestamp_ms <= ready_before_ms)
                .order_by(SignalRecord.timestamp_ms.asc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
    return [
        PendingSignal(
            id=r.id,
            symbol=r.symbol,
            timestamp_ms=r.timestamp_ms,
            price=r.price,
            atr=r.atr,
            # The timeframe the signal was *scored* on. The tracker compares it
            # against the label's own timeframe to decide whether an exact bar
            # match is required or a stated cross-timeframe placement is.
            timeframe=Timeframe(tf) if not isinstance(tf, Timeframe) else tf,
        )
        for r in rows
    ]


def save_moonshot_resolutions(resolutions: list[Resolution]) -> int:
    """Write ×10 verdicts back onto their signal rows. Graded once, never re-graded."""
    if not resolutions:
        return 0
    written = 0
    with get_session() as session:
        for res in resolutions:
            row = session.get(SignalRecord, res.signal_id)
            if row is None or row.moon_outcome_label is not None:
                continue
            row.moon_outcome_label = res.label
            row.moon_outcome_label_config = res.label_config
            row.moon_outcome_horizon_bars = res.horizon_bars
            row.moon_outcome_return_pct = res.return_pct
            row.moon_outcome_net_return_pct = res.net_return_pct
            row.moon_outcome_max_multiple = res.max_multiple
            row.moon_outcome_bars_held = res.bars_held
            row.moon_outcome_entry_price = res.entry_price
            row.moon_outcome_exit_price = res.exit_price
            row.moon_outcome_note = res.note
            row.moon_outcome_evaluated_at = _utcnow()
            written += 1
        session.commit()
    return written


def resolved_moonshot_signals(limit: int | None = None, include_synthetic: bool = True) -> list[dict]:
    """Every ×10 reading carrying a settled verdict."""
    with get_session() as session:
        stmt = (
            select(SignalRecord)
            .where(SignalRecord.moon_outcome_label.in_(["WIN", "LOSS", "TIMEOUT"]))
            .order_by(desc(SignalRecord.timestamp_ms))
        )
        if not include_synthetic:
            stmt = stmt.where(SignalRecord.synthetic.is_(False))
        if limit:
            stmt = stmt.limit(limit)
        rows = session.execute(stmt).scalars().all()
    return [_moonshot_outcome_row(r) for r in rows]


def _moonshot_outcome_row(r: SignalRecord) -> dict:
    return {
        "id": r.id,
        "symbol": r.symbol,
        "timestamp_ms": r.timestamp_ms,
        "price": r.price,
        "moonshot_score": r.moonshot_score,
        "moonshot_stage": r.moonshot_stage,
        "moonshot_ignition": r.moonshot_ignition,
        "moonshot_headroom": r.moonshot_headroom,
        "moonshot_capacity": r.moonshot_capacity,
        "moonshot_multiple_to_high": r.moonshot_multiple_to_high,
        "market_cap_usd": r.market_cap_usd,
        "engine_version": r.moonshot_engine_version,
        "regime": r.market_regime,
        "synthetic": r.synthetic,
        "final_score": r.final_score,
        "pump_maturity": r.pump_maturity,
        "state": r.setup_state,
        "outcome": r.moon_outcome_label,
        "label_config": r.moon_outcome_label_config,
        "return_pct": r.moon_outcome_return_pct,
        "net_return_pct": r.moon_outcome_net_return_pct,
        "max_multiple": r.moon_outcome_max_multiple,
        "bars_held": r.moon_outcome_bars_held,
    }


def moonshot_counts() -> dict:
    """How much ×10 history exists, and how much of it has settled."""
    with get_session() as session:
        def count(*conditions) -> int:
            stmt = select(func.count()).select_from(SignalRecord)
            for c in conditions:
                stmt = stmt.where(c)
            return int(session.execute(stmt).scalar() or 0)

        graded = SignalRecord.moon_outcome_label
        total = count(SignalRecord.moonshot_score.is_not(None))
        settled = count(graded.in_(["WIN", "LOSS", "TIMEOUT"]))
        reached_10x = count(SignalRecord.moon_outcome_max_multiple >= 10.0)
        return {
            "readings_journalled": total,
            "pending_evaluation": count(SignalRecord.moonshot_score.is_not(None), graded.is_(None)),
            "settled": settled,
            "wins": count(graded == "WIN"),
            "losses": count(graded == "LOSS"),
            "timeouts": count(graded == "TIMEOUT"),
            "unresolvable": count(graded == "UNRESOLVABLE"),
            "candidates_journalled": count(SignalRecord.moonshot_stage.in_(["IGNITION", "ACCUMULATION"])),
            # The number the whole layer exists for. Reported even when zero —
            # especially when zero.
            "reached_10x": reached_10x,
        }


def outcome_counts() -> dict:
    """Journal state: how many signals exist, and how many have a verdict."""
    with get_session() as session:
        total = session.query(SignalRecord).count()
        pending = session.query(SignalRecord).filter(SignalRecord.outcome_label.is_(None)).count()
        unresolvable = session.query(SignalRecord).filter(SignalRecord.outcome_label == "UNRESOLVABLE").count()
        wins = session.query(SignalRecord).filter(SignalRecord.outcome_label == "WIN").count()
        losses = session.query(SignalRecord).filter(SignalRecord.outcome_label == "LOSS").count()
        timeouts = session.query(SignalRecord).filter(SignalRecord.outcome_label == "TIMEOUT").count()
        synthetic = session.query(SignalRecord).filter(SignalRecord.synthetic.is_(True)).count()
    settled = wins + losses + timeouts
    return {
        "total_signals": total,
        "pending_evaluation": pending,
        "unresolvable": unresolvable,
        "settled": settled,
        "wins": wins,
        "losses": losses,
        "timeouts": timeouts,
        "synthetic_signals": synthetic,
    }
