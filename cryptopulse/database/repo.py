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
    "prune", "retained_but_unsettled", "last_scan_snapshot",
]

_PERSIST_STATES = {
    SetupState.OBSERVE,
    SetupState.WATCH,
    SetupState.ARMED,
    SetupState.BREAKOUT,
    SetupState.RETEST,
    SetupState.CONTINUATION,
}


def _explosion_columns(r) -> dict:
    """The explosion engine's claim, journalled beside the opportunity score.

    All four columns stay NULL when the engine did not run. Writing a zero
    instead would be indistinguishable from "this token is calm", and the row
    would then be counted in any rate computed later — turning an absence of
    measurement into a measured failure.

    Its own version and fingerprint travel with it for the same reason the
    opportunity score's do: when there are enough 15m horizon rows to fit these
    weights, rows scored under the old ones must not be reinterpreted.
    """
    e = getattr(r, "explosion", None)
    if e is None:
        return {}
    return {
        "explosion_score": round(e.score, 2),
        "explosion_label": e.label,
        "explosion_engine_version": e.engine_version,
        "explosion_weights_fingerprint": e.weights_fingerprint,
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
                        **_explosion_columns(r),
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
                    **_explosion_columns(r),
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
        # NULL on every row written before this engine existed, and on any row
        # it did not score. Carried flat rather than nested so a later query can
        # bucket 15m horizon results by explosion score without unpacking JSON.
        "explosion_score": r.explosion_score,
        "explosion_label": r.explosion_label,
        "explosion_engine_version": r.explosion_engine_version,
        "explosion_weights_fingerprint": r.explosion_weights_fingerprint,
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
            # NULL on rows written before the explosion engine existed. The
            # statistics layer drops them from the explosion buckets rather than
            # treating an unscored row as a zero-scored one.
            "explosion_score": sig.explosion_score,
            "explosion_label": sig.explosion_label,
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


# --------------------------------------------------------------------------- #
# Retention
#
# `CP_DB_RETENTION_DAYS` existed as a setting long before anything applied it,
# so the journal grew without bound. That matters most where the disk is
# smallest — a phone running this under Termux.
#
# Pruning is deliberately asymmetric, because the three tables are worth very
# different amounts:
#
#   * `score_points` and `scan_runs` are high-volume operational noise. One row
#     per symbol per scan means tens of thousands a day, and nothing downstream
#     reads them beyond a short window. They go first and are cut hardest.
#   * `signals` are the point of the whole exercise — they are what makes the
#     scanner evaluable at all. They are only ever dropped past the full
#     retention window, and never while they still owe an answer.
#
# The rule that stops this being destructive: **a signal that has not yet been
# graded, or whose horizon windows are not all recorded, is never deleted.**
# Pruning it would silently remove exactly the rows that were about to become
# evidence, and the loss would look like a quiet journal rather than a bug.
# --------------------------------------------------------------------------- #


def prune(retention_days: int, *, operational_days: int | None = None) -> dict:
    """Drop rows past their retention window. Returns what was removed.

    `operational_days` bounds the cheap high-volume tables and defaults to a
    quarter of the retention window (at least one day).
    """
    from cryptopulse.outcomes.horizons import HORIZONS

    if retention_days <= 0:
        return {"skipped": "retention disabled (retention_days <= 0)"}

    op_days = operational_days if operational_days is not None else max(1, retention_days // 4)
    now_ms = int(_utcnow().timestamp() * 1000)
    signal_cutoff = now_ms - retention_days * 86_400_000
    op_cutoff = now_ms - op_days * 86_400_000
    wanted_horizons = len(HORIZONS)

    with get_session() as session:
        score_points = session.execute(
            delete(ScorePointRecord).where(ScorePointRecord.timestamp_ms < op_cutoff)
        ).rowcount
        scan_runs = session.execute(
            delete(ScanRunRecord).where(ScanRunRecord.started_at_ms < op_cutoff)
        ).rowcount
        alerts = session.execute(
            delete(AlertRecord).where(AlertRecord.timestamp_ms < signal_cutoff)
        ).rowcount

        # Only signals that have finished answering: a verdict recorded AND every
        # horizon window stored. Anything still owed stays, however old it is.
        settled_horizons = (
            select(SignalHorizonRecord.signal_id)
            .group_by(SignalHorizonRecord.signal_id)
            .having(func.count(SignalHorizonRecord.id) >= wanted_horizons)
        )
        doomed = [
            row[0]
            for row in session.execute(
                select(SignalRecord.id).where(
                    SignalRecord.timestamp_ms < signal_cutoff,
                    SignalRecord.outcome_label.is_not(None),
                    SignalRecord.id.in_(settled_horizons),
                )
            ).all()
        ]

        horizons = 0
        if doomed:
            horizons = session.execute(
                delete(SignalHorizonRecord).where(SignalHorizonRecord.signal_id.in_(doomed))
            ).rowcount
            session.execute(delete(SignalRecord).where(SignalRecord.id.in_(doomed)))

        session.commit()

    removed = {
        "signals": len(doomed),
        "signal_horizons": horizons,
        "score_points": score_points,
        "scan_runs": scan_runs,
        "alerts": alerts,
        "retention_days": retention_days,
        "operational_days": op_days,
    }
    log.info("retention_pruned", **removed)
    return removed


def retained_but_unsettled() -> int:
    """Signals held past retention because they still owe a verdict or a window.

    Surfaced so a journal that stops shrinking is understood rather than
    mistaken for a pruning failure.
    """
    from cryptopulse.outcomes.horizons import HORIZONS

    settled = (
        select(SignalHorizonRecord.signal_id)
        .group_by(SignalHorizonRecord.signal_id)
        .having(func.count(SignalHorizonRecord.id) >= len(HORIZONS))
    )
    with get_session() as session:
        return session.execute(
            select(func.count(SignalRecord.id)).where(
                (SignalRecord.outcome_label.is_(None)) | (SignalRecord.id.not_in(settled))
            )
        ).scalar_one()


# --------------------------------------------------------------------------- #
# Warm start
#
# `/api/scan` used to read an in-memory variable that is empty until the first
# scan of the *process* completes. After a restart the dashboard therefore showed
# nothing for as long as a full scan takes — on a phone, hundreds of requests over
# a mobile connection — even though the journal on disk held the previous scan.
#
# This rebuilds the last scan from that journal so the screen is populated
# immediately. Two rules keep it honest:
#
#   * The snapshot carries its own age and its own provenance. It is never
#     presented as live, and a snapshot written by the synthetic provider stays
#     marked synthetic even if the process is now running against a real feed.
#     Showing old DEMO rows without the DEMO banner would be the worst possible
#     regression.
#   * It is a genuine subset. Only signals at OBSERVE or above are journalled, and
#     order-book-derived fields were never stored, so a snapshot row has fewer
#     columns than a live one. Missing values are `None` — never zero, never
#     carried over from a neighbouring row.
# --------------------------------------------------------------------------- #


def last_scan_snapshot(limit: int = 200) -> dict | None:
    """The most recent scan, rebuilt from the journal. `None` if none exists."""
    with get_session() as session:
        run = session.execute(
            select(ScanRunRecord).order_by(desc(ScanRunRecord.started_at_ms)).limit(1)
        ).scalars().first()
        if run is None:
            return None

        # Signals belonging to that run: the scan writes them all with the close
        # time of the candle they were scored on, so the newest batch shares a
        # timestamp. Take the newest distinct timestamp rather than a time range,
        # which would mix two scans when the interval is short.
        newest_ts = session.execute(
            select(func.max(SignalRecord.timestamp_ms))
        ).scalar_one_or_none()
        rows: list[SignalRecord] = []
        if newest_ts is not None:
            rows = list(
                session.execute(
                    select(SignalRecord)
                    .where(SignalRecord.timestamp_ms == newest_ts)
                    .order_by(desc(SignalRecord.final_score))
                    .limit(limit)
                ).scalars().all()
            )

    now_ms = int(_utcnow().timestamp() * 1000)
    # Provenance follows the rows on screen, not the process. If every row came
    # from the synthetic provider, this snapshot is DEMO whatever runs now.
    synthetic = bool(rows[0].synthetic) if rows else bool(run.synthetic)
    provider = rows[0].data_source if rows else run.provider

    return {
        "started_at_ms": run.started_at_ms,
        "signals_at_ms": newest_ts,
        "age_seconds": round((now_ms - (newest_ts or run.started_at_ms)) / 1000, 1),
        "duration_ms": run.duration_ms,
        "universe_size": run.universe_size,
        "scanned": run.scanned,
        "succeeded": run.succeeded,
        "failed": run.failed,
        "provider": provider,
        "synthetic": synthetic,
        "regime": rows[0].market_regime if rows else None,
        "rows": [_snapshot_row(r) for r in rows],
    }


def _snapshot_row(r: SignalRecord) -> dict:
    """One journal row shaped like a live scan row, with holes left as holes."""
    from cryptopulse.scoring.verdict import build_verdict

    return {
        "symbol": r.symbol,
        "price": r.price,
        "timestamp_ms": r.timestamp_ms,
        "engine_version": r.engine_version,
        "weights_fingerprint": r.weights_fingerprint,
        "raw_score": r.raw_score,
        "risk_penalty": r.risk_penalty,
        "final_score": r.final_score,
        "opportunity_label": f"{r.final_score:.0f}/100",
        "score_acceleration": r.score_acceleration,
        "previous_score": None,
        "components": [
            {"name": k, "points": v, "max_points": 0, "fraction": 0.0,
             "available": True, "reasons": [], "detail": {}}
            for k, v in (r.components or {}).items()
        ],
        "penalties": r.penalties or {"total": r.risk_penalty, "items": []},
        "pump_maturity": {"score": r.pump_maturity, "is_late": False, "reasons": []},
        "acceleration": {"momentum_acceleration": 0.0, "early_move": 0.0, "reasons": []},
        "data_confidence": {"score": r.data_confidence, "issues": [], "max_age_seconds": None},
        "liquidity": {"status": r.liquidity_status, "veto": bool(r.hard_veto), "reasons": []},
        "safety": {"score": r.safety_score, "hard_veto": bool(r.hard_veto), "reasons": []},
        "setup": {"state": r.setup_state, "rationale": "", "trigger": None, "invalidation": None},
        "is_premium": bool(r.is_premium),
        "verdict": build_verdict(_JournalRow(r)).to_dict(),
        # Order-book fields were never journalled, so they stay absent rather
        # than being reported as zero.
        "metrics": {
            "rvol": r.rvol,
            "atr_pct": None,
            "distance_to_breakout_atr": r.distance_to_breakout_atr,
            "resistance": r.breakout_level,
        },
        "why": r.why or [],
        "risks": r.risks or [],
        "from_journal": True,
    }


class _JournalRow:
    """Adapter letting `build_verdict` read a stored row.

    `build_verdict` is typed loosely on purpose and touches only fields the
    journal already keeps, so a snapshot verdict is computed by the same code as
    a live one rather than by a second implementation that could drift.
    """

    __slots__ = ("final_score", "risk_penalty", "maturity", "confidence",
                 "liquidity", "safety", "state", "penalties", "is_premium")

    def __init__(self, r: SignalRecord) -> None:
        from types import SimpleNamespace

        from cryptopulse.risk.liquidity import LiquidityStatus
        from cryptopulse.scoring.states import SetupState

        self.final_score = r.final_score
        self.risk_penalty = r.risk_penalty
        self.maturity = SimpleNamespace(score=r.pump_maturity, is_late=r.pump_maturity >= 70.0)
        self.confidence = SimpleNamespace(score=r.data_confidence, issues=[])
        self.liquidity = SimpleNamespace(
            status=LiquidityStatus(r.liquidity_status), veto=bool(r.hard_veto), reasons=[]
        )
        self.safety = SimpleNamespace(score=r.safety_score, hard_veto=bool(r.hard_veto), reasons=[])
        self.state = SimpleNamespace(
            state=SetupState(r.setup_state), rationale="recorded in the journal", trigger=None
        )
        items = (r.penalties or {}).get("items", [])
        self.penalties = SimpleNamespace(
            items=[SimpleNamespace(reason=i.get("reason", "")) for i in items]
        )
        self.is_premium = bool(r.is_premium)
