"""Did any of this work? — personal results and engine performance.

TWO SEPARATE QUESTIONS, DELIBERATELY NOT MERGED

**Did the engine recommend well?** Measured on every BUY it emitted, taken or
not. This is a property of the software.

**Did the user do well?** Measured on the positions they actually opened and
closed. This is a property of a person, and it is not the same number — someone
can follow good signals badly or ignore them profitably.

The comparison between them is the one thing here worth building, and it only
works because a refused signal is kept and followed. Storing only what was acted
on would compare the engine against a sample the user pre-selected, which would
flatter whichever of the two happened to be doing the selecting.

EVERY RATE CARRIES ITS n, AND MOST OF THEM WILL BE ABSENT

This is a new feature on a journal that is days old. Almost every number here
will read "insufficient sample" for months, and that is the correct output
rather than a failure — the same floor of twenty observations that governs every
other statistic in this project governs these. A win rate over four positions is
not a finding about anyone's trading.

WHAT IS NOT COMPUTED, ON PURPOSE

No score for the user. `outcomes/stats.py` reports what the price did; this
reports what was decided and what followed. Grading a person out of a hundred
from a handful of trades would be the most misleading number this product could
produce, and it would be the one people remember.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from cryptopulse.core.logging import get_logger

log = get_logger("trading.stats")

__all__ = ["MIN_SAMPLE", "Bucket", "build_trading_performance"]

# The same floor as everywhere else in this project. Below it, no rate.
MIN_SAMPLE = 20


@dataclass(slots=True)
class Bucket:
    """A set of outcomes with its own n, and no rate until n is enough."""

    key: str
    n: int = 0
    wins: int = 0
    mean_pct: float | None = None
    median_pct: float | None = None
    best_pct: float | None = None
    worst_pct: float | None = None
    mean_win_pct: float | None = None
    mean_loss_pct: float | None = None
    mean_mfe_pct: float | None = None
    mean_mae_pct: float | None = None
    median_minutes_held: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def insufficient_sample(self) -> bool:
        return self.n < MIN_SAMPLE

    @property
    def win_rate(self) -> float | None:
        """Absent below the floor. Not greyed out — absent."""
        return None if self.insufficient_sample or self.n == 0 else self.wins / self.n

    @property
    def profit_factor(self) -> float | None:
        """Gross gains over gross losses. `None` when either side is empty:
        a profit factor computed with no losing trade is infinity, which is a
        statement about the sample rather than about the strategy."""
        if self.insufficient_sample:
            return None
        if self.mean_win_pct is None or self.mean_loss_pct is None:
            return None
        gains = self.mean_win_pct * self.wins
        losses = abs(self.mean_loss_pct) * (self.n - self.wins)
        return None if losses == 0 else gains / losses

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "n": self.n,
            "wins": self.wins,
            "win_rate": _r(self.win_rate),
            "mean_pct": _r(self.mean_pct),
            "median_pct": _r(self.median_pct),
            "best_pct": _r(self.best_pct),
            "worst_pct": _r(self.worst_pct),
            "mean_win_pct": _r(self.mean_win_pct),
            "mean_loss_pct": _r(self.mean_loss_pct),
            "profit_factor": _r(self.profit_factor),
            "mean_mfe_pct": _r(self.mean_mfe_pct),
            "mean_mae_pct": _r(self.mean_mae_pct),
            "median_minutes_held": self.median_minutes_held,
            "insufficient_sample": self.insufficient_sample,
            "min_sample": MIN_SAMPLE,
            "notes": self.notes,
        }


def _r(x, nd: int = 4):
    if x is None:
        return None
    v = float(x)
    return round(v, nd) if np.isfinite(v) else None


def _bucket(key: str, returns: list[float], *, mfe=None, mae=None, held=None) -> Bucket:
    b = Bucket(key=key, n=len(returns))
    if not returns:
        return b

    arr = np.array(returns, dtype=np.float64)
    wins = arr[arr > 0]
    losses = arr[arr <= 0]

    b.wins = int(len(wins))
    b.mean_pct = float(arr.mean())
    b.median_pct = float(np.median(arr))
    b.best_pct = float(arr.max())
    b.worst_pct = float(arr.min())
    b.mean_win_pct = float(wins.mean()) if len(wins) else None
    b.mean_loss_pct = float(losses.mean()) if len(losses) else None
    if mfe:
        b.mean_mfe_pct = float(np.mean(mfe))
    if mae:
        b.mean_mae_pct = float(np.mean(mae))
    if held:
        b.median_minutes_held = int(np.median(held))

    if b.insufficient_sample:
        b.notes.append(
            f"{b.n} observation(s), sous le seuil de {MIN_SAMPLE}. Aucun taux n'est "
            "affiché : un pourcentage sur une poignée de cas n'est pas une mesure."
        )
    return b


def build_trading_performance(positions: list[dict], signals: list[dict]) -> dict:
    """Personal results, engine results, and the comparison between them.

    `positions` are closed positions from `repo.positions("CLOSED")`.
    `signals` are rows from `repo.recent_trade_signals()`.
    """
    closed = [p for p in positions if p.get("realised_pnl_pct") is not None]
    real = [p for p in closed if not p.get("synthetic")]

    overall = _bucket(
        "toutes",
        [p["realised_pnl_pct"] for p in closed],
        mfe=[p["mfe_pct"] for p in closed if p.get("mfe_pct") is not None],
        mae=[p["mae_pct"] for p in closed if p.get("mae_pct") is not None],
        held=[
            (p["closed_ms"] - p["opened_ms"]) / 60_000
            for p in closed
            if p.get("closed_ms") and p.get("opened_ms")
        ],
    )

    buys = [s for s in signals if s["action"] == "BUY"]
    taken = [s for s in buys if s["taken"] is True]
    skipped = [s for s in buys if s["taken"] is False]
    # Unanswered is neither. Counting it as a refusal would turn every prompt
    # nobody got round to into a deliberate decision.
    unanswered = [s for s in buys if s["taken"] is None]

    notes: list[str] = []
    synthetic_positions = len(closed) - len(real)
    if synthetic_positions:
        notes.append(
            f"{synthetic_positions} position(s) sur {len(closed)} ont été prises en mode "
            "DÉMO, sur des bougies générées. Elles ne disent rien d'un marché réel."
        )
    if not closed:
        notes.append(
            "Aucune position clôturée pour l'instant. Ce tableau se remplit quand vous "
            "confirmez une vente, pas avant."
        )
    notes.append(
        "Aucune note n'est donnée à vos décisions. Cet écran dit ce qui a été décidé et "
        "ce qui a suivi ; il ne juge pas votre flair, ce qui demanderait des mois de "
        "décisions réelles pour vouloir dire quoi que ce soit."
    )

    return {
        "positions": {
            "opened": len(positions),
            "closed": len(closed),
            "overall": overall.to_dict(),
            "by_strength": _by_strength(closed, signals),
        },
        "signals": {
            "buy_total": len(buys),
            "taken": len(taken),
            "skipped": len(skipped),
            # Surfaced separately and never folded into "skipped".
            "unanswered": len(unanswered),
            "follow_rate": (
                None
                if len(taken) + len(skipped) == 0
                else round(len(taken) / (len(taken) + len(skipped)), 4)
            ),
        },
        "taken_vs_skipped": _taken_vs_skipped(taken, skipped),
        "sell_signals": _sell_quality(signals),
        "min_sample": MIN_SAMPLE,
        "notes": notes,
    }


def _by_strength(closed: list[dict], signals: list[dict]) -> list[dict]:
    """Do the strong signals actually do better than the standard ones?

    The question the strength grades exist to answer, and the one that will
    eventually justify keeping three of them or collapsing them to one.
    """
    by_id = {s["id"]: s for s in signals}
    groups: dict[str, list[float]] = {}
    for p in closed:
        signal = by_id.get(p.get("signal_id"))
        key = (signal or {}).get("strength") or "SANS_SIGNAL"
        groups.setdefault(key, []).append(p["realised_pnl_pct"])
    return [_bucket(k, v).to_dict() for k, v in sorted(groups.items())]


def _taken_vs_skipped(taken: list[dict], skipped: list[dict]) -> dict:
    """The comparison the whole journal exists to make possible.

    Both sides are measured on the *signal's* own outcome columns rather than on
    what the user realised, so they are the same measurement — comparing a
    realised trade against a theoretical one would be comparing two different
    things and concluding something about the user.
    """
    def side(rows: list[dict], key: str) -> Bucket:
        returns = [
            r["outcome"]["change_1h_pct"]
            for r in rows
            if r.get("outcome", {}).get("change_1h_pct") is not None
        ]
        return _bucket(key, returns)

    followed = side(taken, "signaux suivis")
    ignored = side(skipped, "signaux ignorés")

    delta = None
    if not followed.insufficient_sample and not ignored.insufficient_sample:
        if followed.mean_pct is not None and ignored.mean_pct is not None:
            delta = round(followed.mean_pct - ignored.mean_pct, 4)

    return {
        "taken": followed.to_dict(),
        "skipped": ignored.to_dict(),
        # Absent until both sides clear the floor. A difference between two
        # numbers that are each unreliable is not a finding.
        "mean_difference_pct": delta,
        "note": (
            "Mesuré sur l'évolution du prix 1h après le signal, des deux côtés — la même "
            "mesure. Comparer un gain réalisé à un gain théorique reviendrait à comparer "
            "deux choses différentes et à en conclure quelque chose sur vous."
        ),
    }


def _sell_quality(signals: list[dict]) -> dict:
    """Did selling actually avoid a fall?

    The only honest test of an exit engine: what the price did *after* it said
    to get out. A sell is right when the price fell afterwards, so the sign is
    inverted relative to a buy — stated here because reading this table with a
    buy's intuition would invert the conclusion.
    """
    sells = [s for s in signals if s["action"] == "SELL"]
    windows = {}
    for horizon in ("15m", "1h", "4h", "24h"):
        key = f"change_{horizon}_pct"
        values = [
            s["outcome"][key] for s in sells if s.get("outcome", {}).get(key) is not None
        ]
        bucket = _bucket(horizon, values)
        # A sell "worked" when the price went down, so wins are counted the
        # other way round for this table only.
        if values:
            bucket.wins = sum(1 for v in values if v < 0)
        windows[horizon] = bucket.to_dict()

    return {
        "total": len(sells),
        "by_horizon": windows,
        "note": (
            "Un signal VENDRE a eu raison quand le prix a BAISSÉ ensuite. Le sens est "
            "donc inversé par rapport à un signal ACHETER."
        ),
    }
