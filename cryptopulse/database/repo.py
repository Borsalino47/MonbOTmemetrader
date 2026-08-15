"""Repository: the only module that writes to the database.

The signal journal is what turns this from a scanner into something that can be
evaluated. Every scored asset above OBSERVE is written with its full breakdown so
that, after enough occurrences, it becomes possible to ask which components
actually carried predictive value — rather than assuming the V1 weights were right.
"""

from __future__ import annotations

import json

from sqlalchemy import delete, desc, select
from sqlalchemy.exc import IntegrityError

from cryptopulse.alerts.engine import Alert
from cryptopulse.core.logging import get_logger
from cryptopulse.database.models import AlertRecord, ScanRunRecord, ScorePointRecord, SignalRecord
from cryptopulse.database.session import get_session
from cryptopulse.scanner.base import ScanReport
from cryptopulse.scoring.states import SetupState

log = get_logger("database.repo")

__all__ = ["persist_scan", "persist_alerts", "recent_signals", "score_history", "recent_alerts", "signal_stats"]

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

    Deliberately does not report a win rate: no signal has an outcome yet, and
    computing a rate over NULL outcomes would produce a confident-looking number
    with nothing behind it.
    """
    with get_session() as session:
        total = session.query(SignalRecord).count()
        evaluated = session.query(SignalRecord).filter(SignalRecord.outcome_label.isnot(None)).count()
        wins = session.query(SignalRecord).filter(SignalRecord.outcome_label == "WIN").count()
    return {
        "total_signals": total,
        "evaluated": evaluated,
        "pending_evaluation": total - evaluated,
        "wins": wins,
        "win_rate": round(wins / evaluated, 4) if evaluated else None,
        "win_rate_note": (
            "No outcomes have been evaluated yet. Win rate is null until the outcome "
            "tracker has resolved signals against future price."
            if not evaluated
            else f"Based on {evaluated} resolved signals."
        ),
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
