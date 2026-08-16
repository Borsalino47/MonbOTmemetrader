"""Persistence schema.

Three tables carry the record that makes later validation possible:

* `signals` — every scored asset that reached at least OBSERVE, with the full
  score breakdown and the engine version that produced it.
* `score_points` — the score time series per symbol, which is what score
  acceleration and the "was it rising before it moved?" question are built on.
* `alerts` — what was actually surfaced to the user, so alert quality can be
  measured separately from score quality.

`signals.outcome_*` columns start NULL and are filled in later by the outcome
tracker once enough time has passed. They are never populated at insert time —
that would be look-ahead written straight into the database.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SignalRecord(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    scanner: Mapped[str] = mapped_column(String(16), default="cex")
    timestamp_ms: Mapped[int] = mapped_column(Integer().with_variant(Integer, "sqlite"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    price: Mapped[float] = mapped_column(Float)
    raw_score: Mapped[float] = mapped_column(Float)
    risk_penalty: Mapped[float] = mapped_column(Float)
    final_score: Mapped[float] = mapped_column(Float, index=True)
    score_acceleration: Mapped[float | None] = mapped_column(Float, nullable=True)

    pump_maturity: Mapped[float] = mapped_column(Float)
    data_confidence: Mapped[float] = mapped_column(Float)
    safety_score: Mapped[float] = mapped_column(Float)
    liquidity_status: Mapped[str] = mapped_column(String(16))
    setup_state: Mapped[str] = mapped_column(String(16), index=True)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    hard_veto: Mapped[bool] = mapped_column(Boolean, default=False)

    engine_version: Mapped[str] = mapped_column(String(32), index=True)
    weights_fingerprint: Mapped[str] = mapped_column(String(16))
    data_source: Mapped[str] = mapped_column(String(32), default="unknown")
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    market_regime: Mapped[str | None] = mapped_column(String(24), nullable=True)

    atr: Mapped[float | None] = mapped_column(Float, nullable=True)
    rvol: Mapped[float | None] = mapped_column(Float, nullable=True)
    breakout_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_to_breakout_atr: Mapped[float | None] = mapped_column(Float, nullable=True)

    components: Mapped[dict] = mapped_column(JSON, default=dict)
    penalties: Mapped[dict] = mapped_column(JSON, default=dict)
    why: Mapped[list] = mapped_column(JSON, default=list)
    risks: Mapped[list] = mapped_column(JSON, default=list)

    # --- the explosion engine's separate claim ------------------------------ #
    # Journalled beside the opportunity score, never merged into it. This is the
    # only score in the system whose horizon is already measured: the 15m row in
    # `signal_horizons` records exactly what the price did over the window this
    # number makes a claim about, which is what will eventually make it testable.
    # NULL means the engine did not run for this row — distinct from a zero,
    # which is a statement.
    explosion_score: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    explosion_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    explosion_engine_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    explosion_weights_fingerprint: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # --- filled in later by the outcome tracker; NULL at insert time --------- #
    # These are never written at insert time. Doing so would be look-ahead bias
    # committed straight to disk: the outcome of a signal is not knowable at the
    # moment the signal fires.
    outcome_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome_horizon_bars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # WIN / LOSS / TIMEOUT / UNRESOLVABLE. UNRESOLVABLE means the bars needed to
    # judge it are no longer fetchable — it is excluded from every rate.
    outcome_label: Mapped[str | None] = mapped_column(String(16), nullable=True)
    outcome_label_config: Mapped[str | None] = mapped_column(String(32), nullable=True)
    outcome_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_net_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_mfe_atr: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_mae_atr: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_bars_held: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("symbol", "timestamp_ms", "engine_version", name="uq_signal_symbol_ts_engine"),
        Index("ix_signals_score_time", "final_score", "timestamp_ms"),
    )


class ScorePointRecord(Base):
    __tablename__ = "score_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    timestamp_ms: Mapped[int] = mapped_column(Integer().with_variant(Integer, "sqlite"), index=True)
    final_score: Mapped[float] = mapped_column(Float)
    raw_score: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    state: Mapped[str] = mapped_column(String(16))

    __table_args__ = (UniqueConstraint("symbol", "timestamp_ms", name="uq_scorepoint_symbol_ts"),)


class SignalHorizonRecord(Base):
    """What the price did 15m / 1h / 4h / 24h after a signal.

    One row per (signal, horizon). Separate from the barrier verdict on
    `signals` because they answer different questions: the barrier says what you
    would have traded, this says what the market actually did. A row exists only
    once its window has fully elapsed — a pending horizon is absent, never stored
    as a zero.
    """

    __tablename__ = "signal_horizons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    horizon: Mapped[str] = mapped_column(String(8), index=True)  # 15m / 1h / 4h / 24h
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    status: Mapped[str] = mapped_column(String(16))  # RESOLVED / UNRESOLVABLE
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_at_horizon: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_gain_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    bars_seen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("signal_id", "horizon", name="uq_horizon_signal_window"),
    )


class AlertRecord(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    level: Mapped[str] = mapped_column(String(16), index=True)
    headline: Mapped[str] = mapped_column(String(120))
    timestamp_ms: Mapped[int] = mapped_column(Integer().with_variant(Integer, "sqlite"), index=True)
    dedup_key: Mapped[str] = mapped_column(String(32), index=True)
    price: Mapped[float] = mapped_column(Float)
    final_score: Mapped[float] = mapped_column(Float)
    pump_maturity: Mapped[float] = mapped_column(Float)
    data_confidence: Mapped[float] = mapped_column(Float)
    safety: Mapped[float] = mapped_column(Float)
    state: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class ScanRunRecord(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at_ms: Mapped[int] = mapped_column(Integer().with_variant(Integer, "sqlite"), index=True)
    duration_ms: Mapped[int] = mapped_column(Integer)
    universe_size: Mapped[int] = mapped_column(Integer)
    scanned: Mapped[int] = mapped_column(Integer)
    succeeded: Mapped[int] = mapped_column(Integer)
    failed: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(32))
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    errors: Mapped[str | None] = mapped_column(Text, nullable=True)


class ValidationRecord(Base):
    """A decision the user made about a token, with the state it was made in.

    WHY THE CONTEXT IS COPIED IN RATHER THAN JOINED

    Every other table here records what the software thought. This records what
    the *person* thought, and it is the only place in the project where a human
    judgement becomes data. For that to be worth anything later, the row must
    say what was on screen at the moment of the decision — the price, all three
    scores, the verdict, the reasons and the invalidation the user was looking
    at when they tapped.

    Joining to `signals` instead would look tidier and would be wrong. A signal
    row exists only for states at OBSERVE or above, so validating a token in
    IGNORE would have nothing to join to; the scan that produced the screen may
    have been pruned by the time the decision is analysed; and a later engine
    version would re-explain an old decision in words the user never saw. The
    duplication is the point: this row is a photograph, not a pointer.

    The decision is immutable once written. Changing one's mind creates a new
    row, so a sequence of decisions on the same token stays legible as a
    sequence rather than being flattened into its last state.
    """

    __tablename__ = "validations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    # VALIDATED / REJECTED / WATCHLIST / ANALYSE
    decision: Mapped[str] = mapped_column(String(16), index=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    # The moment on the chart the decision was about, which is not the moment it
    # was taken: a user reading a card at 14:03 is looking at the 14:00 bar.
    signal_timestamp_ms: Mapped[int] = mapped_column(
        Integer().with_variant(Integer, "sqlite"), index=True
    )
    # Set when the screen was showing a journalled signal, NULL otherwise. A
    # convenience for joins, never the source of the context below.
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # --- what was on screen, copied at the moment of the decision ----------- #
    price: Mapped[float] = mapped_column(Float)
    final_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    explosion_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    discovery_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    pump_maturity: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    setup_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    verdict_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    engine_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # The sentences the user actually read, so a decision can be reread in the
    # words that produced it rather than in whatever the engine would say today.
    why: Mapped[list] = mapped_column(JSON, default=list)
    risks: Mapped[list] = mapped_column(JSON, default=list)
    trigger: Mapped[str | None] = mapped_column(String(200), nullable=True)
    invalidation: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note: Mapped[str | None] = mapped_column(String(400), nullable=True)

    # A decision taken on generated candles must never be counted alongside one
    # taken on a real feed, and the distinction has to survive in the row itself.
    data_source: Mapped[str] = mapped_column(String(32), default="unknown")
    synthetic: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # --- filled in later, exactly like the signal outcomes ------------------ #
    # Never written at insert time: what followed a decision is not knowable at
    # the moment it is taken.
    outcome_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    outcome_horizon_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        Index("ix_validations_symbol_time", "symbol", "decided_at"),
    )
