"""Did the Robinhood engines work? Aggregated, with `n` on every rate.

WHY THIS IS KEPT APART FROM `outcomes/stats.py`

Pooling the two markets would answer neither question. A Binance signal and a
Robinhood decision are produced by different engines, over different evidence,
about assets whose failure modes have nothing in common — one can be sold badly,
the other can become unsellable. A combined win rate would move when either
engine changed and would identify neither.

WHAT EACH TABLE IS FOR

  by_action        Does 🟢 ACHETER actually beat 🟡 SURVEILLER, which should
                   beat ⚫ NE PAS ACHETER? If the order is wrong or absent, the
                   floors in `RobinhoodSettings` are wrong. This is the single
                   most important table here.
  by_early_band    Does a higher early score do better? The engine's own claim.
  by_explosion     The 15m row is the explosion engine's scorecard — the only
                   window it makes a claim about (invariant 38).
  by_rug_risk      Sanity: tokens flagged HIGH should not quietly outperform.
  by_age_bucket    Is "younger is better" true, or is it a story?

EVERY RATE CARRIES ITS `n` (invariant 12)

Below `MIN_SAMPLE` the bucket is flagged `insufficient_sample` and the rate is
still shown *with* the count beside it, because on a chain this young the
insufficient case is the ordinary one and hiding it would leave a blank screen
that reads as breakage. The flag is what stops it being read as a finding.

NO RATE IS EVER SHOWN FOR THE USER'S OWN DECISIONS

Same rule as invariant 43. This module reports on the *engine*.
"""

from __future__ import annotations

import numpy as np

from cryptopulse.core.logging import get_logger
from cryptopulse.outcomes.robinhood_outcomes import RH_HORIZONS
from cryptopulse.outcomes.stats import MIN_SAMPLE

log = get_logger("outcomes.robinhood_stats")

__all__ = ["build_robinhood_performance", "MIN_SAMPLE"]

# The window the explosion engine makes its claim about. Named here so the
# table can point at it rather than the reader having to know.
EXPLOSION_CLAIM_HORIZON = "15m"

# Ordered so the screen reads worst-to-best rather than alphabetically.
_ACTION_ORDER = ["BUY", "WATCH", "AVOID", "HOLD", "REDUCE", "SELL"]


def _band(score: float | None, edges: tuple[tuple[float, float], ...]) -> str | None:
    if score is None:
        return None
    for lo, hi in edges:
        if lo <= score < hi:
            return f"{lo:.0f}-{hi:.0f}"
    return None


def _early_band(score: float | None) -> str | None:
    return _band(score, ((0, 40), (40, 60), (60, 75), (75, 101)))


def _explosion_band(score: float | None) -> str | None:
    if score is None:
        return None
    if score <= 0.0:
        return "0 (bloqué)"
    return _band(score, ((1, 45), (45, 70), (70, 101)))


def _age_bucket(seconds: float | None) -> str | None:
    """Unknown age is None, never a bucket.

    Filing an unknown age under "< 15 min" would put it at the top of the list
    people act on fastest (invariant 69).
    """
    if seconds is None:
        return None
    for limit, label in ((900, "< 15 min"), (1800, "< 30 min"), (3600, "< 1 h"),
                         (21600, "< 6 h"), (86400, "< 24 h")):
        if seconds < limit:
            return label
    return "> 24 h"


def _bucket(name: str, rows: list[dict], *, use_net: bool) -> dict:
    """One row of one table. Never a rate without its count."""
    field = "net_change_pct" if use_net else "change_pct"
    values = [r[field] for r in rows if r.get(field) is not None]
    verdicts = [r["success"] for r in rows if r.get("success") is not None]
    n = len(verdicts)

    return {
        "bucket": name,
        "n": n,
        # A rate is meaningless without the count it came from, so they travel
        # together and the flag says when the count is too small to read.
        "success_rate": round(sum(1 for v in verdicts if v) / n, 4) if n else None,
        "insufficient_sample": n < MIN_SAMPLE,
        "median_change_pct": round(float(np.median(values)), 3) if values else None,
        "mean_change_pct": round(float(np.mean(values)), 3) if values else None,
        "best_pct": round(max(values), 3) if values else None,
        "worst_pct": round(min(values), 3) if values else None,
        "median_max_gain_pct": _median(rows, "max_gain_pct"),
        "median_max_drawdown_pct": _median(rows, "max_drawdown_pct"),
    }


def _median(rows: list[dict], key: str) -> float | None:
    values = [r[key] for r in rows if r.get(key) is not None]
    return round(float(np.median(values)), 3) if values else None


def _table(rows: list[dict], keyfn, horizons: tuple[str, ...], *, use_net: bool) -> list[dict]:
    """One table: a bucket per key, per horizon."""
    out: list[dict] = []
    for horizon in horizons:
        window = [r for r in rows if r["horizon"] == horizon]
        groups: dict[str, list[dict]] = {}
        for row in window:
            key = keyfn(row)
            if key is None:
                # An unmeasurable key is dropped from the table rather than
                # bucketed under a made-up label.
                continue
            groups.setdefault(key, []).append(row)
        for key in sorted(groups):
            entry = _bucket(key, groups[key], use_net=use_net)
            entry["horizon"] = horizon
            out.append(entry)
    return out


def build_robinhood_performance(rows: list[dict], *, use_net: bool = True) -> dict:
    """Everything the Verification screen shows for Robinhood Chain.

    `rows` come from `repo.robinhood_horizon_rows()` — one per graded window,
    joined to the decision that produced it.
    """
    horizons = tuple(h.name for h in RH_HORIZONS)
    notes: list[str] = []

    notes.append(
        "Une fenêtre est réussie lorsque la variation depuis l'entrée, après le coût "
        "aller-retour modélisé, dépasse zéro. Stable n'est pas gagnant."
        if use_net
        else "Les variations sont brutes : ni frais, ni glissement ne sont déduits."
    )

    overall = [_overall(rows, h, use_net=use_net) for h in horizons]
    graded = sum(b["n"] for b in overall)
    if graded < MIN_SAMPLE:
        notes.append(
            f"Seulement {graded} fenêtre(s) évaluée(s) au total. En dessous de "
            f"{MIN_SAMPLE}, rien ici ne doit être lu comme un résultat — sur une "
            "chaîne aussi récente, c'est le cas normal."
        )

    notes.append(
        "Le tableau « par décision » est le plus important : 🟢 ACHETER doit faire "
        "mieux que 🟡 SURVEILLER, qui doit faire mieux que ⚫ NE PAS ACHETER. Si "
        "l'ordre n'y est pas, ce sont les planchers qui sont faux."
    )
    notes.append(
        f"La ligne {EXPLOSION_CLAIM_HORIZON} du tableau « par explosion » est le "
        "bulletin du score d'explosion : c'est la seule fenêtre sur laquelle il "
        "avance une affirmation."
    )

    return {
        "market": "ROBINHOOD",
        "chain_id": 4663,
        "horizons": list(horizons),
        "overall": overall,
        "by_action": _sorted_by_action(
            _table(rows, lambda r: r.get("action"), horizons, use_net=use_net)
        ),
        "by_early_band": _table(
            rows, lambda r: _early_band(r.get("early_score")), horizons, use_net=use_net
        ),
        "by_explosion_band": _table(
            rows, lambda r: _explosion_band(r.get("explosion_score")), horizons, use_net=use_net
        ),
        "by_rug_risk": _table(rows, lambda r: r.get("rug_risk"), horizons, use_net=use_net),
        "by_age_bucket": _table(
            rows, lambda r: _age_bucket(r.get("pool_age_seconds")), horizons, use_net=use_net
        ),
        "explosion_claim_horizon": EXPLOSION_CLAIM_HORIZON,
        "return_basis": "net_change_pct" if use_net else "change_pct",
        "min_sample": MIN_SAMPLE,
        "notes": notes,
    }


def _overall(rows: list[dict], horizon: str, *, use_net: bool) -> dict:
    entry = _bucket("toutes décisions", [r for r in rows if r["horizon"] == horizon],
                    use_net=use_net)
    entry["horizon"] = horizon
    return entry


def _sorted_by_action(table: list[dict]) -> list[dict]:
    """BUY before WATCH before AVOID, so the comparison reads top to bottom."""
    order = {name: i for i, name in enumerate(_ACTION_ORDER)}
    return sorted(table, key=lambda b: (b["horizon"], order.get(b["bucket"], 99)))
