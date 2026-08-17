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
    PositionEventRecord,
    PositionRecord,
    ScanRunRecord,
    ScorePointRecord,
    SignalHorizonRecord,
    SignalRecord,
    TradeSignalRecord,
    ValidationRecord,
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
    "save_validation", "recent_validations", "latest_validation_per_symbol",
    "validation_counts", "VALID_DECISIONS",
    "save_trade_signal", "answer_trade_signal", "recent_trade_signals",
    "unanswered_trade_signals", "open_position", "update_position",
    "record_position_decision", "close_position", "positions", "position_by_id",
    "open_position_symbols", "position_events",
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
            "Aucune issue n'a encore été tranchée. Le taux de réussite reste vide tant que "
            "le suivi des résultats n'a pas évalué les signaux contre les bougies qui les "
            "ont suivis."
        )
    elif settled < MIN_SAMPLE:
        note = (
            f"Basé sur seulement {settled} signal(x) réglé(s) — en dessous du minimum de "
            f"{MIN_SAMPLE}. À lire comme un test de bon fonctionnement du pipeline, pas "
            "comme une preuve concernant la stratégie."
        )
    else:
        note = f"Basé sur {settled} signal(x) réglé(s) (gagnant / perdant / sans issue)."

    if counts["synthetic_signals"]:
        note += (
            f" {counts['synthetic_signals']} signal(x) proviennent du fournisseur "
            "SYNTHÉTIQUE et décrivent des données générées, pas un marché."
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


# --------------------------------------------------------------------------- #
# Validations — the only human judgements this system stores.
#
# Everything else here records what the software thought. These record what the
# user decided, and they are the one dataset that can eventually answer "does
# the person using this outperform the scanner, or the other way round?" — a
# question no amount of internal scoring can answer on its own.
# --------------------------------------------------------------------------- #

VALID_DECISIONS = ("VALIDATED", "REJECTED", "WATCHLIST", "ANALYSE")


def save_validation(payload: dict) -> dict:
    """Record one decision. Returns the stored row.

    Appends rather than upserts. A user who validates a token and rejects it an
    hour later has made two decisions, and flattening them into a final state
    would destroy exactly the sequence worth studying.
    """
    decision = str(payload.get("decision", "")).upper()
    if decision not in VALID_DECISIONS:
        raise ValueError(
            f"unknown decision {decision!r}; expected one of {', '.join(VALID_DECISIONS)}"
        )

    with get_session() as session:
        row = ValidationRecord(
            symbol=str(payload["symbol"]).upper(),
            decision=decision,
            signal_timestamp_ms=int(payload["signal_timestamp_ms"]),
            signal_id=payload.get("signal_id"),
            price=float(payload["price"]),
            final_score=payload.get("final_score"),
            explosion_score=payload.get("explosion_score"),
            discovery_score=payload.get("discovery_score"),
            pump_maturity=payload.get("pump_maturity"),
            data_confidence=payload.get("data_confidence"),
            setup_state=payload.get("setup_state"),
            verdict_level=payload.get("verdict_level"),
            engine_version=payload.get("engine_version"),
            why=(payload.get("why") or [])[:12],
            risks=(payload.get("risks") or [])[:12],
            trigger=_clip(payload.get("trigger"), 200),
            invalidation=_clip(payload.get("invalidation"), 200),
            note=_clip(payload.get("note"), 400),
            data_source=payload.get("data_source") or "unknown",
            synthetic=bool(payload.get("synthetic", False)),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _validation_to_dict(row)


def recent_validations(
    limit: int = 100, *, symbol: str | None = None, decision: str | None = None
) -> list[dict]:
    with get_session() as session:
        stmt = select(ValidationRecord).order_by(desc(ValidationRecord.decided_at)).limit(limit)
        if symbol:
            stmt = stmt.where(ValidationRecord.symbol == symbol.upper())
        if decision:
            stmt = stmt.where(ValidationRecord.decision == decision.upper())
        return [_validation_to_dict(r) for r in session.execute(stmt).scalars().all()]


def latest_validation_per_symbol(limit: int = 200) -> dict[str, dict]:
    """The most recent decision for each symbol, for marking rows on screen.

    Deliberately derived rather than stored: the table is append-only, so "the
    current state" is a view over the history and can never drift from it.
    """
    latest: dict[str, dict] = {}
    for row in recent_validations(limit):
        latest.setdefault(row["symbol"], row)
    return latest


def validation_counts() -> dict:
    with get_session() as session:
        rows = session.execute(
            select(ValidationRecord.decision, func.count(ValidationRecord.id)).group_by(
                ValidationRecord.decision
            )
        ).all()
        synthetic = session.execute(
            select(func.count(ValidationRecord.id)).where(ValidationRecord.synthetic.is_(True))
        ).scalar_one()
    counts = {d: 0 for d in VALID_DECISIONS}
    counts.update({d: n for d, n in rows})
    return {
        "counts": counts,
        "total": sum(counts.values()),
        # Decisions taken on generated candles say nothing about the user's
        # judgement of a market, and must never be pooled with the real ones.
        "synthetic": synthetic,
    }


def _clip(value, length: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:length] if text else None


def _validation_to_dict(r: ValidationRecord) -> dict:
    return {
        "id": r.id,
        "symbol": r.symbol,
        "decision": r.decision,
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "signal_timestamp_ms": r.signal_timestamp_ms,
        "signal_id": r.signal_id,
        "price": r.price,
        "final_score": r.final_score,
        "explosion_score": r.explosion_score,
        "discovery_score": r.discovery_score,
        "pump_maturity": r.pump_maturity,
        "data_confidence": r.data_confidence,
        "setup_state": r.setup_state,
        "verdict_level": r.verdict_level,
        "engine_version": r.engine_version,
        "why": r.why,
        "risks": r.risks,
        "trigger": r.trigger,
        "invalidation": r.invalidation,
        "note": r.note,
        "data_source": r.data_source,
        "synthetic": r.synthetic,
        "outcome": {
            "evaluated": r.outcome_evaluated_at is not None,
            "horizon_minutes": r.outcome_horizon_minutes,
            "price": r.outcome_price,
            "change_pct": r.outcome_change_pct,
            "note": r.outcome_note,
        },
    }


# --------------------------------------------------------------------------- #
# Trading — signals emitted, positions held, decisions changed.
#
# Every write here is append-or-update on a row the user's own action created.
# Nothing in this section can place an order; it records what a person did after
# reading a recommendation.
# --------------------------------------------------------------------------- #


def save_trade_signal(payload: dict) -> dict:
    """Record a BUY or SELL recommendation. `taken` starts NULL, not False.

    Unanswered is a third state and it matters: folding it into "no" would count
    every prompt the user never got round to as a deliberate refusal, and the
    comparison in §26 between taken and skipped signals would be wrong from the
    first day.
    """
    action = str(payload.get("action", "")).upper()
    if action not in ("BUY", "SELL"):
        raise ValueError(f"a trade signal is BUY or SELL, got {action!r}")

    with get_session() as session:
        row = TradeSignalRecord(
            symbol=str(payload["symbol"]).upper(),
            action=action,
            strength=payload.get("strength"),
            timestamp_ms=int(payload["timestamp_ms"]),
            price=float(payload["price"]),
            opportunity_score=payload.get("opportunity_score"),
            explosion_score=payload.get("explosion_score"),
            discovery_score=payload.get("discovery_score"),
            safety_score=payload.get("safety_score"),
            data_confidence=payload.get("data_confidence"),
            pump_maturity=payload.get("pump_maturity"),
            setup_state=payload.get("setup_state"),
            market_regime=payload.get("market_regime"),
            trigger_price=payload.get("trigger_price"),
            invalidation_price=payload.get("invalidation_price"),
            reasons=(payload.get("reasons") or [])[:8],
            risks=(payload.get("risks") or [])[:8],
            engine_version=payload.get("engine_version") or "unknown",
            weights_fingerprint=payload.get("weights_fingerprint") or "",
            data_source=payload.get("data_source") or "unknown",
            synthetic=bool(payload.get("synthetic", False)),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _trade_signal_to_dict(row)


def answer_trade_signal(signal_id: int, taken: bool, *, position_id: int | None = None) -> dict:
    """Record the user's OUI / NON. Answering twice keeps the first answer.

    A signal is a moment: the question "did you act on this?" has one true
    answer, and letting it be rewritten later would turn the taken/not-taken
    comparison into a record of how someone feels about their past decisions.
    """
    with get_session() as session:
        row = session.get(TradeSignalRecord, signal_id)
        if row is None:
            raise LookupError(f"no trade signal with id {signal_id}")
        if row.taken is None:
            row.taken = bool(taken)
            row.answered_at = _utcnow()
        # The position link is not part of the answer, so it can still be
        # attached afterwards. Found by the API test: opening a position answers
        # the signal first and only knows the position id once the row exists,
        # so refusing the second write left every position unlinked from the
        # recommendation that produced it.
        if position_id is not None and row.position_id is None:
            row.position_id = position_id
        session.commit()
        session.refresh(row)
        return _trade_signal_to_dict(row)


def recent_trade_signals(
    limit: int = 100, *, action: str | None = None, taken: bool | None = None
) -> list[dict]:
    with get_session() as session:
        stmt = select(TradeSignalRecord).order_by(desc(TradeSignalRecord.timestamp_ms)).limit(limit)
        if action:
            stmt = stmt.where(TradeSignalRecord.action == action.upper())
        if taken is not None:
            stmt = stmt.where(TradeSignalRecord.taken.is_(taken))
        return [_trade_signal_to_dict(r) for r in session.execute(stmt).scalars().all()]


def unanswered_trade_signals(limit: int = 20) -> list[dict]:
    """Prompts still waiting for an OUI / NON, newest first."""
    with get_session() as session:
        stmt = (
            select(TradeSignalRecord)
            .where(TradeSignalRecord.taken.is_(None))
            .order_by(desc(TradeSignalRecord.timestamp_ms))
            .limit(limit)
        )
        return [_trade_signal_to_dict(r) for r in session.execute(stmt).scalars().all()]


def open_position(payload: dict) -> dict:
    """Create a position from a confirmed purchase.

    Peak and trough start at the entry price rather than at NULL: a position
    that has never been observed since entry has a peak — it is where it
    started. Leaving them NULL would make the first MFE reading look like a
    gain measured from nowhere.
    """
    with get_session() as session:
        entry = float(payload["entry_price"])
        # Seeded from the same price the returns are computed against, not from
        # the observed one. Found by running it: with a fill of 1.23 against an
        # observed 2.17, the trough started above the basis and "perte max"
        # rendered as +75.79% — a maximum loss that was a gain.
        basis = float(payload.get("actual_entry_price") or entry)
        row = PositionRecord(
            symbol=str(payload["symbol"]).upper(),
            chain=payload.get("chain") or "CEX",
            contract_address=_clip(payload.get("contract_address"), 80),
            signal_id=payload.get("signal_id"),
            opened_ms=int(payload["opened_ms"]),
            entry_price=entry,
            actual_entry_price=payload.get("actual_entry_price"),
            amount_invested=payload.get("amount_invested"),
            quantity=payload.get("quantity"),
            trigger_price=payload.get("trigger_price"),
            invalidation_price=payload.get("invalidation_price"),
            entry_opportunity=payload.get("entry_opportunity"),
            entry_explosion=payload.get("entry_explosion"),
            entry_discovery=payload.get("entry_discovery"),
            entry_safety=payload.get("entry_safety"),
            entry_confidence=payload.get("entry_confidence"),
            entry_maturity=payload.get("entry_maturity"),
            entry_rvol=payload.get("entry_rvol"),
            entry_state=payload.get("entry_state"),
            entry_regime=payload.get("entry_regime"),
            entry_reasons=(payload.get("entry_reasons") or [])[:8],
            last_price=entry,
            last_seen_ms=int(payload["opened_ms"]),
            peak_price=basis,
            trough_price=basis,
            mfe_pct=0.0,
            mae_pct=0.0,
            data_source=payload.get("data_source") or "unknown",
            synthetic=bool(payload.get("synthetic", False)),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _position_to_dict(row)


def update_position(position_id: int, *, price: float, now_ms: int, health: float | None = None) -> dict:
    """Record the current price and widen the peak / trough.

    Peak and trough only ever widen. A watcher that misses a cycle therefore
    loses resolution and never a record — the worst case is an MFE slightly
    understated, never a fabricated one.
    """
    with get_session() as session:
        row = session.get(PositionRecord, position_id)
        if row is None:
            raise LookupError(f"no position with id {position_id}")

        row.last_price = price
        row.last_seen_ms = now_ms
        row.peak_price = price if row.peak_price is None else max(row.peak_price, price)
        row.trough_price = price if row.trough_price is None else min(row.trough_price, price)

        basis = row.actual_entry_price or row.entry_price
        if basis:
            row.mfe_pct = (row.peak_price - basis) / basis * 100.0
            row.mae_pct = (row.trough_price - basis) / basis * 100.0
        if health is not None:
            row.health_score = health

        session.commit()
        session.refresh(row)
        return _position_to_dict(row)


def record_position_decision(
    position_id: int, payload: dict, *, now_ms: int
) -> dict | None:
    """Append a decision change. Returns the event, or None if nothing changed.

    Only *changes* are stored. Writing a row every cycle would bury the five
    moments that matter under a thousand that do not, and the sequence is the
    entire value of this table.
    """
    decision = str(payload["decision"]).upper()
    with get_session() as session:
        row = session.get(PositionRecord, position_id)
        if row is None:
            raise LookupError(f"no position with id {position_id}")
        previous = row.current_decision
        if previous == decision:
            return None

        row.current_decision = decision
        row.decision_changed_ms = now_ms
        event = PositionEventRecord(
            position_id=position_id,
            symbol=row.symbol,
            at_ms=now_ms,
            decision=decision,
            previous_decision=previous,
            price=float(payload["price"]),
            pnl_pct=payload.get("pnl_pct"),
            health_score=payload.get("health_score"),
            opportunity_score=payload.get("opportunity_score"),
            explosion_score=payload.get("explosion_score"),
            reasons=(payload.get("reasons") or [])[:8],
            risks=(payload.get("risks") or [])[:8],
        )
        session.add(event)
        session.commit()
        session.refresh(event)
        return _position_event_to_dict(event)


def close_position(
    position_id: int,
    *,
    exit_price: float,
    now_ms: int,
    actual_exit_price: float | None = None,
    reason: str | None = None,
) -> dict:
    """Close a position. Idempotent: closing a closed position changes nothing.

    The realised PnL uses the actual fills where the user supplied them and the
    observed prices otherwise, and `pnl_basis` says which — a return computed
    from screen prices is a different number from one computed from fills, and
    they must never be pooled without knowing which is which.
    """
    with get_session() as session:
        row = session.get(PositionRecord, position_id)
        if row is None:
            raise LookupError(f"no position with id {position_id}")
        if row.status == "CLOSED":
            return _position_to_dict(row)

        row.status = "CLOSED"
        row.closed_at = _utcnow()
        row.closed_ms = now_ms
        row.exit_price = exit_price
        row.actual_exit_price = actual_exit_price
        row.close_reason = _clip(reason, 200)

        entry = row.actual_entry_price or row.entry_price
        exit_used = actual_exit_price if actual_exit_price is not None else exit_price
        if entry:
            row.realised_pnl_pct = (exit_used - entry) / entry * 100.0

        session.commit()
        session.refresh(row)
        return _position_to_dict(row)


def positions(status: str | None = "OPEN", limit: int = 200) -> list[dict]:
    with get_session() as session:
        stmt = select(PositionRecord).order_by(desc(PositionRecord.opened_ms)).limit(limit)
        if status:
            stmt = stmt.where(PositionRecord.status == status.upper())
        return [_position_to_dict(r) for r in session.execute(stmt).scalars().all()]


def position_by_id(position_id: int) -> dict | None:
    with get_session() as session:
        row = session.get(PositionRecord, position_id)
        return _position_to_dict(row) if row else None


def open_position_symbols() -> list[str]:
    """What the position watcher has to follow. One cheap query per cycle."""
    with get_session() as session:
        rows = session.execute(
            select(PositionRecord.symbol).where(PositionRecord.status == "OPEN").distinct()
        ).all()
        return [r[0] for r in rows]


def position_events(position_id: int, limit: int = 200) -> list[dict]:
    with get_session() as session:
        rows = (
            session.execute(
                select(PositionEventRecord)
                .where(PositionEventRecord.position_id == position_id)
                .order_by(PositionEventRecord.at_ms)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return [_position_event_to_dict(r) for r in rows]


# --------------------------------------------------------------------------- #


def _trade_signal_to_dict(r: TradeSignalRecord) -> dict:
    return {
        "id": r.id,
        "symbol": r.symbol,
        "action": r.action,
        "strength": r.strength,
        "emitted_at": r.emitted_at.isoformat() if r.emitted_at else None,
        "timestamp_ms": r.timestamp_ms,
        "price": r.price,
        # None means "not answered yet" and is never rendered as a no.
        "taken": r.taken,
        "answered_at": r.answered_at.isoformat() if r.answered_at else None,
        "position_id": r.position_id,
        "opportunity_score": r.opportunity_score,
        "explosion_score": r.explosion_score,
        "discovery_score": r.discovery_score,
        "safety_score": r.safety_score,
        "data_confidence": r.data_confidence,
        "pump_maturity": r.pump_maturity,
        "setup_state": r.setup_state,
        "market_regime": r.market_regime,
        "trigger_price": r.trigger_price,
        "invalidation_price": r.invalidation_price,
        "reasons": r.reasons,
        "risks": r.risks,
        "engine_version": r.engine_version,
        "weights_fingerprint": r.weights_fingerprint,
        "data_source": r.data_source,
        "synthetic": r.synthetic,
        "outcome": {
            "evaluated": r.outcome_evaluated_at is not None,
            "change_5m_pct": r.change_5m_pct,
            "change_15m_pct": r.change_15m_pct,
            "change_1h_pct": r.change_1h_pct,
            "change_4h_pct": r.change_4h_pct,
            "change_24h_pct": r.change_24h_pct,
            "mfe_pct": r.mfe_pct,
            "mae_pct": r.mae_pct,
        },
    }


def _position_to_dict(r: PositionRecord) -> dict:
    basis = r.actual_entry_price or r.entry_price
    price = r.last_price
    pnl = None if (basis in (None, 0) or price is None) else (price - basis) / basis * 100.0
    drawdown = (
        None
        if (r.peak_price in (None, 0) or price is None)
        else (price - r.peak_price) / r.peak_price * 100.0
    )
    return {
        "id": r.id,
        "symbol": r.symbol,
        "chain": r.chain,
        "contract_address": r.contract_address,
        "signal_id": r.signal_id,
        "status": r.status,
        "opened_at": r.opened_at.isoformat() if r.opened_at else None,
        "opened_ms": r.opened_ms,
        "entry_price": r.entry_price,
        "actual_entry_price": r.actual_entry_price,
        # Which price the returns are computed from. A return measured on screen
        # prices is a different number from one measured on fills.
        "pnl_basis": "actual_fill" if r.actual_entry_price else "observed_price",
        "amount_invested": r.amount_invested,
        "quantity": r.quantity,
        "trigger_price": r.trigger_price,
        "invalidation_price": r.invalidation_price,
        "entry_opportunity": r.entry_opportunity,
        "entry_explosion": r.entry_explosion,
        "entry_discovery": r.entry_discovery,
        "entry_safety": r.entry_safety,
        "entry_confidence": r.entry_confidence,
        "entry_maturity": r.entry_maturity,
        "entry_rvol": r.entry_rvol,
        "entry_state": r.entry_state,
        "entry_regime": r.entry_regime,
        "entry_reasons": r.entry_reasons,
        "last_price": price,
        "last_seen_ms": r.last_seen_ms,
        "peak_price": r.peak_price,
        "trough_price": r.trough_price,
        "pnl_pct": None if pnl is None else round(pnl, 3),
        "drawdown_from_peak_pct": None if drawdown is None else round(drawdown, 3),
        "mfe_pct": None if r.mfe_pct is None else round(r.mfe_pct, 3),
        "mae_pct": None if r.mae_pct is None else round(r.mae_pct, 3),
        "health_score": r.health_score,
        "current_decision": r.current_decision,
        "decision_changed_ms": r.decision_changed_ms,
        "closed_at": r.closed_at.isoformat() if r.closed_at else None,
        "closed_ms": r.closed_ms,
        "exit_price": r.exit_price,
        "actual_exit_price": r.actual_exit_price,
        "realised_pnl_pct": None if r.realised_pnl_pct is None else round(r.realised_pnl_pct, 3),
        "close_reason": r.close_reason,
        "data_source": r.data_source,
        "synthetic": r.synthetic,
    }


def _position_event_to_dict(r: PositionEventRecord) -> dict:
    return {
        "id": r.id,
        "position_id": r.position_id,
        "symbol": r.symbol,
        "at": r.at.isoformat() if r.at else None,
        "at_ms": r.at_ms,
        "decision": r.decision,
        "previous_decision": r.previous_decision,
        "price": r.price,
        "pnl_pct": r.pnl_pct,
        "health_score": r.health_score,
        "opportunity_score": r.opportunity_score,
        "explosion_score": r.explosion_score,
        "reasons": r.reasons,
        "risks": r.risks,
    }
