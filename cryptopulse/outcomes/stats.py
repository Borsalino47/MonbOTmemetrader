"""Performance analytics over resolved signals.

The point of the journal (spec §29): after enough occurrences, determine which
factors actually carry predictive value — rather than continuing to assume the
V1 weights were right.

Every figure here obeys three rules:

* **`n` travels with every rate.** A 100% win rate over three signals is not a
  finding, and a bucket that reports the rate without the count invites reading
  it as one.
* **Below `MIN_SAMPLE`, buckets are marked `insufficient_sample`** and the caller
  is expected to show them greyed out or not at all. They are still returned,
  because hiding them entirely would make the sample look larger than it is.
* **Nothing is annualised or extrapolated.** These are descriptions of what has
  happened, not projections.

`component_edge` is the interesting one: for each scoring component it compares
the average points awarded to winners against losers. A component that scores
winners and losers identically is contributing noise to the final score, however
sensible it looked when it was written.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

__all__ = [
    "MIN_SAMPLE",
    "Bucket",
    "PerformanceReport",
    "build_performance",
    "HorizonBucket",
    "build_horizon_performance",
]

# Below this many settled signals a bucket's rate is not worth reading.
MIN_SAMPLE = 20


@dataclass(slots=True)
class Bucket:
    key: str
    n: int
    wins: int
    losses: int
    timeouts: int
    win_rate: float | None
    expectancy_pct: float | None
    avg_win_pct: float | None
    avg_loss_pct: float | None
    profit_factor: float | None
    avg_mfe_atr: float | None
    avg_mae_atr: float | None

    @property
    def insufficient_sample(self) -> bool:
        return self.n < MIN_SAMPLE

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "n": self.n,
            "wins": self.wins,
            "losses": self.losses,
            "timeouts": self.timeouts,
            "win_rate": _r(self.win_rate),
            "expectancy_pct": _r(self.expectancy_pct),
            "avg_win_pct": _r(self.avg_win_pct),
            "avg_loss_pct": _r(self.avg_loss_pct),
            "profit_factor": _r(self.profit_factor),
            "avg_mfe_atr": _r(self.avg_mfe_atr),
            "avg_mae_atr": _r(self.avg_mae_atr),
            "insufficient_sample": self.insufficient_sample,
        }


def _r(x, nd: int = 4):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if np.isfinite(v) else None


def _bucket(key: str, rows: list[dict], return_field: str) -> Bucket:
    n = len(rows)
    if n == 0:
        return Bucket(key, 0, 0, 0, 0, None, None, None, None, None, None, None)

    rets = np.array([r.get(return_field) or 0.0 for r in rows], dtype=np.float64)
    wins = [r for r in rows if r["outcome"] == "WIN"]
    losses = [r for r in rows if r["outcome"] == "LOSS"]
    timeouts = [r for r in rows if r["outcome"] == "TIMEOUT"]

    pos = rets[rets > 0]
    neg = rets[rets < 0]
    gross_profit = float(pos.sum()) if pos.size else 0.0
    gross_loss = float(abs(neg.sum())) if neg.size else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

    mfe = [r["mfe_atr"] for r in rows if r.get("mfe_atr") is not None]
    mae = [r["mae_atr"] for r in rows if r.get("mae_atr") is not None]

    return Bucket(
        key=key,
        n=n,
        wins=len(wins),
        losses=len(losses),
        timeouts=len(timeouts),
        win_rate=len(wins) / n,
        expectancy_pct=float(rets.mean()),
        avg_win_pct=float(pos.mean()) if pos.size else None,
        avg_loss_pct=float(neg.mean()) if neg.size else None,
        profit_factor=profit_factor,
        avg_mfe_atr=float(np.mean(mfe)) if mfe else None,
        avg_mae_atr=float(np.mean(mae)) if mae else None,
    )


def _score_band(score: float) -> str:
    for lo, hi in ((35, 50), (50, 65), (65, 80), (80, 101)):
        if lo <= score < hi:
            return f"{lo}-{hi - 1 if hi == 101 else hi}"
    return "<35"


# The bands the explosion engine's own labels use, so the table reads in the
# same vocabulary as the screen rather than in a second, private one.
EXPLOSION_CLAIM_HORIZON = "15m"


def _explosion_band(score: float) -> str:
    if score <= 0.0:
        return "0 (bloqué)"
    for lo, hi in ((1, 45), (45, 70), (70, 101)):
        if lo <= score < hi:
            return f"{lo}-{hi - 1 if hi == 101 else hi}"
    return "unknown"


def _maturity_band(score: float) -> str:
    for lo, hi in ((0, 25), (25, 50), (50, 70), (70, 101)):
        if lo <= score < hi:
            return f"{lo}-{hi - 1 if hi == 101 else hi}"
    return "unknown"


@dataclass(slots=True)
class PerformanceReport:
    overall: Bucket
    by_score_band: list[Bucket] = field(default_factory=list)
    by_state: list[Bucket] = field(default_factory=list)
    by_maturity_band: list[Bucket] = field(default_factory=list)
    by_liquidity: list[Bucket] = field(default_factory=list)
    by_regime: list[Bucket] = field(default_factory=list)
    by_symbol: list[Bucket] = field(default_factory=list)
    component_edge: list[dict] = field(default_factory=list)
    return_basis: str = "net_return_pct"
    synthetic_included: bool = True
    synthetic_count: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "overall": self.overall.to_dict(),
            "by_score_band": [b.to_dict() for b in self.by_score_band],
            "by_state": [b.to_dict() for b in self.by_state],
            "by_maturity_band": [b.to_dict() for b in self.by_maturity_band],
            "by_liquidity": [b.to_dict() for b in self.by_liquidity],
            "by_regime": [b.to_dict() for b in self.by_regime],
            "by_symbol": [b.to_dict() for b in self.by_symbol],
            "component_edge": self.component_edge,
            "return_basis": self.return_basis,
            "min_sample": MIN_SAMPLE,
            "synthetic_included": self.synthetic_included,
            "synthetic_count": self.synthetic_count,
            "notes": self.notes,
        }


def _component_edge(rows: list[dict]) -> list[dict]:
    """Average points awarded to winners vs losers, per scoring component.

    A positive `edge` means the component scored eventual winners higher than
    eventual losers on this sample. It is a description, not a proof: with a
    small `n` the difference is mostly noise, which is why `n` is reported and
    the caller is told when the sample is too small to read.
    """
    wins = [r for r in rows if r["outcome"] == "WIN"]
    losses = [r for r in rows if r["outcome"] == "LOSS"]
    if not wins or not losses:
        return []

    names: set[str] = set()
    for r in rows:
        names.update((r.get("components") or {}).keys())

    out: list[dict] = []
    for name in sorted(names):
        w = [float(r["components"][name]) for r in wins if name in (r.get("components") or {})]
        l = [float(r["components"][name]) for r in losses if name in (r.get("components") or {})]
        if not w or not l:
            continue
        avg_w, avg_l = float(np.mean(w)), float(np.mean(l))
        out.append(
            {
                "component": name,
                "avg_points_winners": round(avg_w, 3),
                "avg_points_losers": round(avg_l, 3),
                "edge": round(avg_w - avg_l, 3),
                "n_winners": len(w),
                "n_losers": len(l),
                "insufficient_sample": (len(w) + len(l)) < MIN_SAMPLE,
            }
        )
    out.sort(key=lambda d: d["edge"], reverse=True)
    return out


def build_performance(
    rows: list[dict], *, use_net: bool = True, synthetic_included: bool = True
) -> PerformanceReport:
    """Aggregate resolved signals into the performance view.

    `rows` must already be filtered to settled outcomes (WIN / LOSS / TIMEOUT).
    """
    return_field = "net_return_pct" if use_net else "return_pct"
    notes: list[str] = []
    synthetic_count = sum(1 for r in rows if r.get("synthetic"))

    if synthetic_count:
        notes.append(
            f"{synthetic_count} of {len(rows)} settled signals came from the SYNTHETIC provider. "
            "Those measure the pipeline, not the strategy."
        )
    if use_net:
        notes.append("Returns are net of the modelled round-trip cost (fees + slippage + half-spread).")
    if len(rows) < MIN_SAMPLE:
        notes.append(
            f"Only {len(rows)} settled signals. Below {MIN_SAMPLE} nothing here should be read as a finding."
        )

    def group(field_name: str, keyfn=None) -> list[Bucket]:
        groups: dict[str, list[dict]] = {}
        for r in rows:
            raw = r.get(field_name)
            if raw is None:
                continue
            key = keyfn(raw) if keyfn else str(raw)
            groups.setdefault(key, []).append(r)
        return [_bucket(k, v, return_field) for k, v in sorted(groups.items())]

    by_symbol = group("symbol")
    by_symbol.sort(key=lambda b: b.n, reverse=True)

    return PerformanceReport(
        overall=_bucket("overall", rows, return_field),
        by_score_band=group("final_score", _score_band),
        by_state=group("state"),
        by_maturity_band=group("pump_maturity", _maturity_band),
        by_liquidity=group("liquidity"),
        by_regime=group("regime"),
        by_symbol=by_symbol[:25],
        component_edge=_component_edge(rows),
        return_basis=return_field,
        synthetic_included=synthetic_included,
        synthetic_count=synthetic_count,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Multi-horizon follow-up statistics
#
# A different question from the barrier report above. The barrier asks "would
# this trade have won?"; this asks "what did the price actually do 15 minutes,
# an hour, four hours and a day later?". Both are kept because a signal can be
# a barrier LOSS and still have been up 2% an hour in — that is information
# about *timing*, and it is invisible in a win rate.
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class HorizonBucket:
    """One horizon (or one slice of one), aggregated.

    `success_rate` uses exactly the criterion in `HorizonResult.is_success`:
    net change above zero. The rate is computed over rows that carry a verdict,
    never over rows that are pending or unresolvable.
    """

    key: str
    horizon: str
    n: int
    successes: int
    success_rate: float | None
    avg_change_pct: float | None
    median_change_pct: float | None
    best_change_pct: float | None
    worst_change_pct: float | None
    avg_max_gain_pct: float | None
    avg_max_drawdown_pct: float | None

    @property
    def insufficient_sample(self) -> bool:
        return self.n < MIN_SAMPLE

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "horizon": self.horizon,
            "n": self.n,
            "successes": self.successes,
            "success_rate": _r(self.success_rate),
            "avg_change_pct": _r(self.avg_change_pct),
            "median_change_pct": _r(self.median_change_pct),
            "best_change_pct": _r(self.best_change_pct),
            "worst_change_pct": _r(self.worst_change_pct),
            "avg_max_gain_pct": _r(self.avg_max_gain_pct),
            "avg_max_drawdown_pct": _r(self.avg_max_drawdown_pct),
            "insufficient_sample": self.insufficient_sample,
        }


def _horizon_bucket(key: str, horizon: str, rows: list[dict], use_net: bool) -> HorizonBucket:
    field_name = "net_change_pct" if use_net else "change_pct"
    graded = [r for r in rows if r.get("success") is not None and r.get(field_name) is not None]
    n = len(graded)
    if n == 0:
        return HorizonBucket(key, horizon, 0, 0, None, None, None, None, None, None, None)

    changes = np.array([float(r[field_name]) for r in graded], dtype=np.float64)
    gains = [float(r["max_gain_pct"]) for r in graded if r.get("max_gain_pct") is not None]
    dds = [float(r["max_drawdown_pct"]) for r in graded if r.get("max_drawdown_pct") is not None]
    successes = sum(1 for r in graded if r["success"])

    return HorizonBucket(
        key=key,
        horizon=horizon,
        n=n,
        successes=successes,
        success_rate=successes / n,
        avg_change_pct=float(changes.mean()),
        median_change_pct=float(np.median(changes)),
        best_change_pct=float(changes.max()),
        worst_change_pct=float(changes.min()),
        avg_max_gain_pct=float(np.mean(gains)) if gains else None,
        avg_max_drawdown_pct=float(np.mean(dds)) if dds else None,
    )


def build_horizon_performance(
    rows: list[dict], *, use_net: bool = True, horizons: tuple[str, ...] | None = None
) -> dict:
    """Aggregate settled horizon windows overall and by score band / provider.

    `rows` are the dicts returned by `repo.horizon_rows()`. Rows without a
    verdict are dropped by `_horizon_bucket` rather than counted as failures.
    """
    if horizons is None:
        from cryptopulse.outcomes.horizons import HORIZONS

        horizons = tuple(h.name for h in HORIZONS)

    by_horizon: dict[str, list[dict]] = {h: [] for h in horizons}
    for r in rows:
        by_horizon.setdefault(r["horizon"], []).append(r)

    def slice_by(field_name: str, keyfn=None) -> list[dict]:
        out: list[dict] = []
        for h in horizons:
            groups: dict[str, list[dict]] = {}
            for r in by_horizon.get(h, []):
                raw = r.get(field_name)
                if raw is None:
                    continue
                groups.setdefault(keyfn(raw) if keyfn else str(raw), []).append(r)
            out.extend(_horizon_bucket(k, h, v, use_net).to_dict() for k, v in sorted(groups.items()))
        return out

    synthetic_count = sum(1 for r in rows if r.get("synthetic"))
    notes: list[str] = []
    if synthetic_count:
        notes.append(
            f"{synthetic_count} of {len(rows)} horizon windows came from the SYNTHETIC provider. "
            "Those measure the pipeline, not the market."
        )
    notes.append(
        "A horizon counts as a success when the change from entry, after the modelled "
        "round-trip cost, is above zero. Flat is not a win."
        if use_net
        else "Changes are gross: no fee, spread or slippage is deducted."
    )

    overall = [_horizon_bucket("overall", h, by_horizon.get(h, []), use_net).to_dict() for h in horizons]
    total_graded = sum(b["n"] for b in overall)
    if total_graded < MIN_SAMPLE:
        notes.append(
            f"Only {total_graded} graded horizon windows in total. Below {MIN_SAMPLE} "
            "nothing here should be read as a finding."
        )

    return {
        "horizons": list(horizons),
        "overall": overall,
        "by_score_band": slice_by("final_score", _score_band),
        "by_state": slice_by("state"),
        "by_provider": slice_by("provider"),
        # The one table in this project that can falsify a score rather than
        # merely describe it. The explosion engine claims a move within fifteen
        # minutes; the 15m row here is what the price actually did over exactly
        # that window. If its high band does not beat its low band, the engine is
        # wrong and this is where that becomes visible.
        #
        # `slice_by` drops rows whose explosion_score is None, so signals written
        # before the engine existed are absent rather than counted as zeros.
        "by_explosion_band": slice_by("explosion_score", _explosion_band),
        "explosion_note": (
            f"The {EXPLOSION_CLAIM_HORIZON} row of this table is the explosion score's own "
            "scorecard: it is the only window the engine makes a claim about. A high band "
            "that does not beat a low band means the weights are wrong, and no other table "
            "here can show that."
        ),
        "return_basis": "net_change_pct" if use_net else "change_pct",
        "min_sample": MIN_SAMPLE,
        "synthetic_count": synthetic_count,
        "notes": notes,
    }
