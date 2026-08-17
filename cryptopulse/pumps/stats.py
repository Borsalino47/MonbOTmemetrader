"""What a token's past accelerations add up to, and whether now resembles them.

TWO QUESTIONS, AND THE SECOND ONE IS DANGEROUS

The first is descriptive: how big were this token's pumps, how fast, how much did
they give back. That is arithmetic over episodes and cannot mislead as long as
the sample size travels with it.

The second is the one worth having and the one easiest to get wrong: *does the
current setup resemble the setups that preceded past pumps?* Three rules keep it
from becoming a fortune teller:

1. **Comparison uses only what was knowable at the time.** Each past episode is
   described by its start context — volume state, volatility, position in range —
   computed in `pumps/detect.py` strictly from bars at or before the start. If
   it used anything from during the run, the finding would be circular: pumps
   are preceded by pumps.

2. **A rate below `MIN_SAMPLE` is not shown.** Most tokens will have five to
   fifteen episodes in 41 days, which is under the floor. The honest output is
   then "not enough comparable setups", not a percentage over six observations.
   This will be the common case and the UI must not treat it as an error.

3. **Similarity is a distance, and it is reported.** "17 comparable setups" is
   only meaningful if the reader knows how alike they had to be. The threshold
   and the number of episodes that failed it are both in the output.

None of this is a probability. It is a count of what happened to setups that
looked like this one, on one token, over a few weeks.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from cryptopulse.core.logging import get_logger
from cryptopulse.outcomes.stats import MIN_SAMPLE
from cryptopulse.pumps.detect import SIZE_BUCKETS, PumpEpisode

log = get_logger("pumps.stats")

__all__ = ["PumpStats", "summarise", "SetupFingerprint", "SimilarityReport", "compare_to_past"]

# Two setups are "comparable" when their normalised distance is under this.
# Deliberately generous: with a few dozen episodes a strict threshold produces
# an empty sample, and an empty sample is worse than a loose one that says so.
SIMILARITY_THRESHOLD = 1.0


@dataclass(slots=True)
class PumpStats:
    n: int
    mean_gain_pct: float | None
    median_gain_pct: float | None
    largest_gain_pct: float | None
    smallest_gain_pct: float | None
    median_minutes_to_peak: int | None
    mean_drawdown_after_pct: float | None
    median_rvol_at_start: float | None
    median_volume_change_before_pct: float | None
    resolution_minutes: int | None
    by_size: dict[str, int] = field(default_factory=dict)

    @property
    def insufficient_sample(self) -> bool:
        return self.n < MIN_SAMPLE

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "mean_gain_pct": _r(self.mean_gain_pct),
            "median_gain_pct": _r(self.median_gain_pct),
            "largest_gain_pct": _r(self.largest_gain_pct),
            "smallest_gain_pct": _r(self.smallest_gain_pct),
            "median_minutes_to_peak": self.median_minutes_to_peak,
            "mean_drawdown_after_pct": _r(self.mean_drawdown_after_pct),
            "median_rvol_at_start": _r(self.median_rvol_at_start),
            "median_volume_change_before_pct": _r(self.median_volume_change_before_pct),
            "resolution_minutes": self.resolution_minutes,
            "by_size": self.by_size,
            "insufficient_sample": self.insufficient_sample,
            "min_sample": MIN_SAMPLE,
        }


def _r(x, nd: int = 3):
    if x is None:
        return None
    v = float(x)
    return round(v, nd) if np.isfinite(v) else None


def _median_or_none(values: list[float]) -> float | None:
    return float(np.median(values)) if values else None


def summarise(episodes: list[PumpEpisode]) -> PumpStats:
    """Descriptive statistics over episodes. Empty in, empty out — never zeros."""
    if not episodes:
        return PumpStats(
            n=0, mean_gain_pct=None, median_gain_pct=None, largest_gain_pct=None,
            smallest_gain_pct=None, median_minutes_to_peak=None,
            mean_drawdown_after_pct=None, median_rvol_at_start=None,
            median_volume_change_before_pct=None, resolution_minutes=None,
        )

    gains = [e.gain_pct for e in episodes]
    drawdowns = [e.drawdown_after_pct for e in episodes if e.drawdown_after_pct is not None]
    rvols = [e.rvol_at_start for e in episodes if e.rvol_at_start is not None]
    vol_changes = [
        e.volume_change_before_pct for e in episodes if e.volume_change_before_pct is not None
    ]

    by_size: dict[str, int] = {}
    for threshold in SIZE_BUCKETS:
        count = sum(1 for e in episodes if e.reached(threshold))
        if count:
            by_size[f"{threshold:.0f} %+"] = count

    return PumpStats(
        n=len(episodes),
        mean_gain_pct=float(np.mean(gains)),
        median_gain_pct=float(np.median(gains)),
        largest_gain_pct=float(np.max(gains)),
        smallest_gain_pct=float(np.min(gains)),
        median_minutes_to_peak=int(np.median([e.minutes_to_peak for e in episodes])),
        mean_drawdown_after_pct=float(np.mean(drawdowns)) if drawdowns else None,
        median_rvol_at_start=_median_or_none(rvols),
        median_volume_change_before_pct=_median_or_none(vol_changes),
        resolution_minutes=episodes[0].resolution_minutes,
        by_size=by_size,
    )


# --------------------------------------------------------------------------- #
# Similarity
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SetupFingerprint:
    """The few measures a setup is compared on. Any may be unknown."""

    rvol: float | None = None
    volume_change_pct: float | None = None
    range_position: float | None = None
    atr_pct: float | None = None

    @classmethod
    def from_episode(cls, e: PumpEpisode) -> SetupFingerprint:
        return cls(
            rvol=e.rvol_at_start,
            volume_change_pct=e.volume_change_before_pct,
            range_position=e.range_position_at_start,
            atr_pct=e.atr_pct_at_start,
        )

    def distance(self, other: SetupFingerprint) -> float | None:
        """Normalised distance, or `None` when too little overlaps to compare.

        Each axis is divided by a scale that makes a typical difference about
        1.0, so no single axis dominates by virtue of its units.
        """
        pairs = [
            (self.rvol, other.rvol, 1.5),
            (self.volume_change_pct, other.volume_change_pct, 80.0),
            (self.range_position, other.range_position, 0.35),
            (self.atr_pct, other.atr_pct, 1.2),
        ]
        terms = [
            ((a - b) / scale) ** 2 for a, b, scale in pairs if a is not None and b is not None
        ]
        if len(terms) < 2:
            # One axis in common is not a comparison, it is a coincidence.
            return None
        return float(np.sqrt(sum(terms) / len(terms)))


@dataclass(slots=True)
class SimilarityReport:
    comparable: int = 0
    examined: int = 0
    not_comparable: int = 0
    threshold: float = SIMILARITY_THRESHOLD
    reached: dict[str, float] = field(default_factory=dict)   # "5%+" -> share
    median_gain_pct: float | None = None
    median_minutes_to_peak: int | None = None
    median_drawdown_after_pct: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def insufficient_sample(self) -> bool:
        return self.comparable < MIN_SAMPLE

    def to_dict(self) -> dict:
        return {
            "comparable": self.comparable,
            "examined": self.examined,
            "not_comparable": self.not_comparable,
            "similarity_threshold": self.threshold,
            # Rates are omitted entirely below the sample floor rather than
            # shown greyed out: a number on screen gets read, however it is styled.
            "reached": self.reached if not self.insufficient_sample else {},
            "median_gain_pct": _r(self.median_gain_pct) if not self.insufficient_sample else None,
            "median_minutes_to_peak": (
                self.median_minutes_to_peak if not self.insufficient_sample else None
            ),
            "median_drawdown_after_pct": (
                _r(self.median_drawdown_after_pct) if not self.insufficient_sample else None
            ),
            "insufficient_sample": self.insufficient_sample,
            "min_sample": MIN_SAMPLE,
            "notes": self.notes,
        }


def compare_to_past(
    current: SetupFingerprint,
    episodes: list[PumpEpisode],
    *,
    threshold: float = SIMILARITY_THRESHOLD,
) -> SimilarityReport:
    """How setups resembling `current` turned out, on this token's own history."""
    report = SimilarityReport(examined=len(episodes), threshold=threshold)

    if not episodes:
        report.notes.append("Aucun épisode passé auquel se comparer")
        return report

    matched: list[PumpEpisode] = []
    for e in episodes:
        d = current.distance(SetupFingerprint.from_episode(e))
        if d is None:
            report.not_comparable += 1
        elif d <= threshold:
            matched.append(e)

    report.comparable = len(matched)

    if report.not_comparable:
        report.notes.append(
            f"{report.not_comparable} épisode(s) passé(s) n'ont pas assez de contexte "
            "enregistré pour être comparés."
        )

    if report.insufficient_sample:
        report.notes.append(
            f"Seulement {report.comparable} setup(s) comparable(s), en dessous des "
            f"{MIN_SAMPLE} nécessaires. Aucun taux n'est affiché : un pourcentage sur une "
            "poignée d'observations n'est pas une preuve. C'est le cas normal sur un token "
            "qui n'a que quelques semaines d'historique."
        )
        return report

    gains = [e.gain_pct for e in matched]
    drawdowns = [e.drawdown_after_pct for e in matched if e.drawdown_after_pct is not None]
    for t in SIZE_BUCKETS:
        share = sum(1 for e in matched if e.reached(t)) / len(matched)
        if share > 0:
            report.reached[f"{t:.0f} %+"] = round(share, 4)

    report.median_gain_pct = float(np.median(gains))
    report.median_minutes_to_peak = int(np.median([e.minutes_to_peak for e in matched]))
    report.median_drawdown_after_pct = float(np.mean(drawdowns)) if drawdowns else None
    report.notes.append(
        f"{report.comparable} setups passés sur ce token ressemblaient à celui du moment. "
        "Ceci décrit ce qui les a suivis ; ce n'est pas une probabilité."
    )
    return report
