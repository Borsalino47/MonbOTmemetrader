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
from cryptopulse.database.models import (
    AlertRecord,
    ScanRunRecord,
    ScorePointRecord,
    SignalHorizonRecord,
    SignalRecord,
    _utcnow,
)
from cryptopulse.database.session import get_session
from cryptopulse.scanner.base import ScanReport
from cryptopulse.scoring.states import SetupState

if TYPE_CHECKING:  # avoids a circular import at runtime: outcomes -> backtest -> ...
    from cryptopulse.outcomes.horizons import HorizonResult
    from cryptopulse.outcomes.tracker import PendingSignal, Resolution

log = get_logger("database.repo")

__all__ = [
    "persist_scan", "persist_alerts", "recent_signals", "score_history", "recent_alerts", "signal_stats",
    "pending_signals", "save_resolutions", "resolved_signals", "outcome_counts",
    "signals_needing_horizons", "save_horizons", "horizon_rows", "horizons_for_signal",
]

_PERSIST_STATES = {
    SetupState.OBSERVE,
    SetupState.WATCH,
    SetupState.ARMED,
    SetupState.BREAKOUT,
    SetupState.RETEST,
    SetupState.CONTINUATION,
}


def persist_scan(report: ScanReport, *, provider: str, regime: str | None = None) -> int:
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
            if r.state.state in _PERSIST_STATES:
                st = r.features.primary.structure if r.features else None
                session.add(
                    SignalRecord(
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
                        synthetic=report.synthetic_data,
                        market_regime=regime,
                        atr=r.features.primary.atr14 if r.features else None,
                        rvol=r.features.primary.rvol if r.features else None,
                        breakout_level=st.nearest_resistance.price if (st and st.nearest_resistance) else None,
                        distance_to_breakout_atr=st.distance_to_resistance_atr if st else None,
                        components={c.name: round(c.points, 2) for c in r.components},
                        penalties=r.penalties.to_dict(),
                        why=r.why()[:12],
                        risks=r.risks()[:12],
                    )
                )
                written += 1

        try:
            session.commit()
        except IntegrityError:
            # A re-scan inside the same candle produces the same (symbol, ts).
            session.rollback()
            written = _commit_individually(report, provider, regime)

    return written


def _commit_individually(report: ScanReport, provider: str, regime: str | None) -> int:
    """Fallback after a batch conflict: insert row by row, skipping duplicates."""
    written = 0
    for r in report.results:
        if r.state.state not in _PERSIST_STATES:
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
            st = r.features.primary.structure if r.features else None
            session.add(
                SignalRecord(
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
                    synthetic=report.synthetic_data,
                    market_regime=regime,
                    atr=r.features.primary.atr14 if r.features else None,
                    rvol=r.features.primary.rvol if r.features else None,
                    breakout_level=st.nearest_resistance.price if (st and st.nearest_resistance) else None,
                    distance_to_breakout_atr=st.distance_to_resistance_atr if st else None,
                    components={c.name: round(c.points, 2) for c in r.components},
                    penalties=r.penalties.to_dict(),
                    why=r.why()[:12],
                    risks=r.risks()[:12],
                )
            )
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


# --------------------------------------------------------------------------- #
# Multi-horizon follow-up
# --------------------------------------------------------------------------- #


def signals_needing_horizons(ready_before_ms: int, limit: int = 300) -> list[PendingSignal]:
    """Signals old enough for at least their first horizon, still missing one.

    A signal keeps coming back until all four windows are stored, so the 24h
    result lands a day after the 15m one without needing a separate schedule.
    """
    from cryptopulse.config.settings import get_settings
    from cryptopulse.core.types import Timeframe
    from cryptopulse.outcomes.horizons import HORIZONS
    from cryptopulse.outcomes.tracker import PendingSignal

    tf = get_settings().scanner.primary_timeframe
    wanted = len(HORIZONS)

    with get_session() as session:
        done = dict(
            session.execute(
                select(SignalHorizonRecord.signal_id, func.count(SignalHorizonRecord.id))
                .group_by(SignalHorizonRecord.signal_id)
            ).all()
        )
        rows = (
            session.execute(
                select(SignalRecord)
                .where(SignalRecord.timestamp_ms <= ready_before_ms)
                .order_by(SignalRecord.timestamp_ms.asc())
                .limit(limit * 3)
            )
            .scalars()
            .all()
        )

    out = []
    for r in rows:
        if done.get(r.id, 0) >= wanted:
            continue  # every window already stored
        out.append(
            PendingSignal(
                id=r.id, symbol=r.symbol, timestamp_ms=r.timestamp_ms,
                price=r.price, atr=r.atr,
                timeframe=tf if isinstance(tf, Timeframe) else Timeframe(tf),
            )
        )
        if len(out) >= limit:
            break
    return out


def save_horizons(results: list[HorizonResult]) -> int:
    """Store settled horizon windows. PENDING ones are skipped, never persisted.

    A horizon is written once and never rewritten: the window has closed, so the
    numbers are historical fact.
    """
    from cryptopulse.outcomes.horizons import HorizonStatus

    written = 0
    with get_session() as session:
        for res in results:
            if res.status is HorizonStatus.PENDING:
                continue
            exists = session.execute(
                select(SignalHorizonRecord.id).where(
                    SignalHorizonRecord.signal_id == res.signal_id,
                    SignalHorizonRecord.horizon == res.horizon,
                )
            ).first()
            if exists:
                continue
            session.add(
                SignalHorizonRecord(
                    signal_id=res.signal_id,
                    symbol=res.symbol,
                    horizon=res.horizon,
                    status=res.status.value,
                    entry_price=res.entry_price,
                    price_at_horizon=res.price_at_horizon,
                    change_pct=res.change_pct,
                    net_change_pct=res.net_change_pct,
                    max_gain_pct=res.max_gain_pct,
                    max_drawdown_pct=res.max_drawdown_pct,
                    bars_seen=res.bars_seen,
                    success=res.is_success,
                    note=res.note,
                )
            )
            written += 1
        session.commit()
    return written


def horizon_rows(limit: int | None = None, include_synthetic: bool = True) -> list[dict]:
    """Settled horizon windows joined to the score that produced them."""
    with get_session() as session:
        stmt = (
            select(SignalHorizonRecord, SignalRecord)
            .join(SignalRecord, SignalRecord.id == SignalHorizonRecord.signal_id)
            .where(SignalHorizonRecord.status == "RESOLVED")
            .order_by(desc(SignalRecord.timestamp_ms))
        )
        if not include_synthetic:
            stmt = stmt.where(SignalRecord.synthetic.is_(False))
        if limit:
            stmt = stmt.limit(limit)
        pairs = session.execute(stmt).all()

    return [
        {
            "signal_id": h.signal_id,
            "symbol": h.symbol,
            "horizon": h.horizon,
            "timestamp_ms": sig.timestamp_ms,
            "change_pct": h.change_pct,
            "net_change_pct": h.net_change_pct,
            "max_gain_pct": h.max_gain_pct,
            "max_drawdown_pct": h.max_drawdown_pct,
            "success": h.success,
            "final_score": sig.final_score,
            "state": sig.setup_state,
            "provider": sig.data_source,
            "regime": sig.market_regime,
            "synthetic": sig.synthetic,
        }
        for h, sig in pairs
    ]


def horizons_for_signal(signal_id: int) -> list[dict]:
    with get_session() as session:
        rows = (
            session.execute(
                select(SignalHorizonRecord)
                .where(SignalHorizonRecord.signal_id == signal_id)
                .order_by(SignalHorizonRecord.id)
            ).scalars().all()
        )
    return [
        {
            "horizon": r.horizon, "status": r.status,
            "price_at_horizon": r.price_at_horizon,
            "change_pct": r.change_pct, "net_change_pct": r.net_change_pct,
            "max_gain_pct": r.max_gain_pct, "max_drawdown_pct": r.max_drawdown_pct,
            "success": r.success, "note": r.note,
        }
        for r in rows
    ]
