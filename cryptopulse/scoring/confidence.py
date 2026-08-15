"""Data Confidence Score — how much the other numbers can be trusted.

An opportunity score of 83 computed from a feed that went stale nine minutes ago,
with no order book and 60 bars of history, is not the same claim as an 83
computed from fresh, complete, multi-timeframe data. Confidence is the honesty
channel: it never improves a signal, it only tells you how much to discount it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptopulse.config.settings import ScannerSettings
from cryptopulse.core.types import DataQuality
from cryptopulse.features.pipeline import AssetFeatures
from cryptopulse.features.stats import clamp01

__all__ = ["DataConfidence", "compute_confidence"]


@dataclass(slots=True)
class DataConfidence:
    score: float  # 0..100
    components: dict[str, float] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    max_age_seconds: float | None = None

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "issues": self.issues,
            "max_age_seconds": None if self.max_age_seconds is None else round(self.max_age_seconds, 1),
        }


_WEIGHTS = {
    "freshness": 0.30,
    "history_depth": 0.20,
    "timeframe_coverage": 0.20,
    "continuity": 0.15,
    "order_book": 0.10,
    "feature_completeness": 0.05,
}


def compute_confidence(af: AssetFeatures, cfg: ScannerSettings, now_ms: int) -> DataConfidence:
    parts: dict[str, float] = {}
    issues: list[str] = []

    # -- freshness ---------------------------------------------------------- #
    # Measured against the close time of the newest closed candle on the primary
    # timeframe. One timeframe-length of lag is normal and costs nothing; beyond
    # the configured tolerance the score collapses.
    primary = af.primary
    tf_ms = primary.timeframe.ms
    age_s = max(0.0, (now_ms - primary.close_time_ms) / 1000.0)
    tolerance = cfg.stale_after_seconds + tf_ms / 1000.0
    parts["freshness"] = clamp01(1.0 - max(0.0, age_s - tf_ms / 1000.0) / max(tolerance, 1.0))
    if age_s > tolerance:
        issues.append(f"STALE_DATA: primary candle closed {age_s:.0f}s ago")

    # Reported age is the *primary* timeframe's, not the max across timeframes.
    # A 4h candle is legitimately up to four hours old; reporting that as "data
    # age" would make every healthy feed look stale.
    max_age = age_s

    # -- history depth ------------------------------------------------------ #
    depth = primary.bars / max(cfg.candles_per_timeframe, 1)
    parts["history_depth"] = clamp01(depth)
    if primary.bars < cfg.min_candles_required:
        issues.append(f"INSUFFICIENT_HISTORY: {primary.bars} closed bars")

    # -- timeframe coverage ------------------------------------------------- #
    wanted = len(cfg.timeframes)
    got = len(af.timeframes)
    parts["timeframe_coverage"] = clamp01(got / wanted) if wanted else 0.0
    if got < wanted:
        issues.append(f"only {got}/{wanted} timeframes available")

    # -- continuity (gaps in the candle stream) ----------------------------- #
    quality_penalty = 0.0
    for f in af.timeframes.values():
        if f.provenance and f.provenance.quality is DataQuality.PARTIAL:
            quality_penalty += 0.25
            issues.append(f"{f.timeframe.value}: partial series")
        if f.warnings:
            quality_penalty += 0.10
    parts["continuity"] = clamp01(1.0 - quality_penalty)

    # -- order book --------------------------------------------------------- #
    parts["order_book"] = 1.0 if af.order_book_imbalance is not None else 0.0
    if af.order_book_imbalance is None:
        issues.append("order book DATA_UNAVAILABLE")

    # -- feature completeness ----------------------------------------------- #
    checks = [primary.rvol, primary.atr14, primary.rsi14, primary.compression, primary.ema20]
    parts["feature_completeness"] = clamp01(sum(1 for c in checks if c is not None) / len(checks))

    score = 100.0 * sum(_WEIGHTS[k] * v for k, v in parts.items())

    # Staleness is a *cap*, not just another weighted term. Without this, an
    # asset with deep history and full timeframe coverage still scores ~60 on a
    # feed that died hours ago, because the other five components are perfect.
    # Age is not one opinion among six: if the data is old, nothing computed from
    # it is trustworthy, however complete it looks.
    if age_s > tolerance:
        overshoot = (age_s - tolerance) / max(tolerance, 1.0)
        cap = max(10.0, 40.0 / (1.0 + overshoot))
        if cap < score:
            issues.append(f"confidence capped at {cap:.0f} because the data is {age_s:.0f}s old")
            score = cap

    return DataConfidence(score=score, components=parts, issues=issues, max_age_seconds=max_age)
