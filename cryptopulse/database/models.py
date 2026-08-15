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

    # --- filled in later by the outcome tracker; NULL at insert time --------- #
    outcome_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome_horizon_bars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome_label: Mapped[str | None] = mapped_column(String(16), nullable=True)  # WIN / LOSS / TIMEOUT
    outcome_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_mfe_atr: Mapped[float | None] = mapped_column(Float, nullable=True)
    outcome_mae_atr: Mapped[float | None] = mapped_column(Float, nullable=True)

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
