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
    "MIN_SAMPLE", "Bucket", "PerformanceReport", "build_performance",
    "MoonshotPerformanceReport", "build_moonshot_performance", "MULTIPLE_RUNGS",
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
# The ×10 axis
#
# A separate report because the question is different. On the intraday axis the
# question is "how often does a signal make 2R". Here it is "how far did these
# actually go" — a distribution, not a rate. A layer whose win rate at ×2 looks
# respectable while nothing ever passed ×3 has not found what it claims to look
# for, and only the distribution shows that.
# --------------------------------------------------------------------------- #

# The rungs the distribution is reported at. ×10 is last and is reported even
# when it is zero — especially when it is zero.
MULTIPLE_RUNGS = (1.5, 2.0, 3.0, 5.0, 10.0)


@dataclass(slots=True)
class MoonshotPerformanceReport:
    overall: Bucket
    by_score_band: list[Bucket]
    by_stage: list[Bucket]
    by_capacity_known: list[Bucket]
    multiple_distribution: list[dict]
    best_multiple: float | None
    median_multiple: float | None
    label_config: str
    return_basis: str
    synthetic_included: bool
    synthetic_count: int
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "overall": self.overall.to_dict(),
            "by_score_band": [b.to_dict() for b in self.by_score_band],
            "by_stage": [b.to_dict() for b in self.by_stage],
            "by_capacity_known": [b.to_dict() for b in self.by_capacity_known],
            "multiple_distribution": self.multiple_distribution,
            "best_multiple": _r(self.best_multiple, 3),
            "median_multiple": _r(self.median_multiple, 3),
            "label_config": self.label_config,
            "return_basis": self.return_basis,
            "min_sample": MIN_SAMPLE,
            "synthetic_included": self.synthetic_included,
            "synthetic_count": self.synthetic_count,
            "notes": self.notes,
        }


def build_moonshot_performance(
    rows: list[dict], *, use_net: bool = True, synthetic_included: bool = True
) -> MoonshotPerformanceReport:
    """Aggregate settled ×10 readings.

    `rows` come from `repo.resolved_moonshot_signals` and are already filtered to
    settled verdicts.
    """
    return_field = "net_return_pct" if use_net else "return_pct"
    notes: list[str] = []
    synthetic_count = sum(1 for r in rows if r.get("synthetic"))
    label = next((r.get("label_config") for r in rows if r.get("label_config")), "") or ""

    if synthetic_count:
        notes.append(
            f"{synthetic_count} of {len(rows)} settled readings came from the SYNTHETIC provider. "
            "Those measure the pipeline, not the market."
        )
    if len(rows) < MIN_SAMPLE:
        notes.append(
            f"Only {len(rows)} settled readings. Below {MIN_SAMPLE} nothing here is a finding, and "
            "a x10 layer needs far more than that before its weights mean anything."
        )

    multiples = [r["max_multiple"] for r in rows if r.get("max_multiple") is not None]
    distribution = [
        {
            "at_least": rung,
            "n": sum(1 for m in multiples if m >= rung),
            "share": (sum(1 for m in multiples if m >= rung) / len(multiples)) if multiples else None,
        }
        for rung in MULTIPLE_RUNGS
    ]
    if multiples and not any(m >= 10.0 for m in multiples):
        notes.append(
            f"No reading has reached x10. The best was x{max(multiples):.2f} — which is the honest "
            "headline for this layer until one does."
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

    # Does knowing the market cap actually separate winners from losers? Until a
    # real history exists this is the cheapest way to find out whether the
    # capacity reading earns the second network dependency it costs.
    capacity_groups: dict[str, list[dict]] = {"capacity known": [], "capacity unknown": []}
    for r in rows:
        capacity_groups["capacity known" if r.get("moonshot_capacity") is not None else "capacity unknown"].append(r)
    by_capacity = [_bucket(k, v, return_field) for k, v in capacity_groups.items() if v]

    return MoonshotPerformanceReport(
        overall=_bucket("overall", rows, return_field),
        by_score_band=group("moonshot_score", _score_band),
        by_stage=group("moonshot_stage"),
        by_capacity_known=by_capacity,
        multiple_distribution=distribution,
        best_multiple=max(multiples) if multiples else None,
        median_multiple=float(np.median(multiples)) if multiples else None,
        label_config=label,
        return_basis=return_field,
        synthetic_included=synthetic_included,
        synthetic_count=synthetic_count,
        notes=notes,
    )
