"""Setup state machine.

The state answers "where in its life is this setup?", which is a different
question from "how good is it?". A 78-scoring asset that has not yet cleared its
level (ARMED) and a 78-scoring asset three bars into a breakout (BREAKOUT) call
for different actions.

State is derived from structure geometry first and score second. Score alone
cannot distinguish ARMED from BREAKOUT — only the price's position relative to
the level can.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from cryptopulse.config.settings import ScoringSettings
from cryptopulse.features.pipeline import AssetFeatures, Bias
from cryptopulse.features.structure import StructureTrend

__all__ = ["SetupState", "StateDecision", "determine_state"]


class SetupState(str, Enum):
    IGNORE = "IGNORE"
    OBSERVE = "OBSERVE"
    WATCH = "WATCH"
    ARMED = "ARMED"
    BREAKOUT = "BREAKOUT"
    RETEST = "RETEST"
    CONTINUATION = "CONTINUATION"
    INVALIDATED = "INVALIDATED"


@dataclass(slots=True)
class StateDecision:
    state: SetupState
    rationale: str
    trigger: str | None = None
    invalidation: str | None = None

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "rationale": self.rationale,
            "trigger": self.trigger,
            "invalidation": self.invalidation,
        }


def determine_state(
    af: AssetFeatures,
    final_score: float,
    cfg: ScoringSettings,
    *,
    hard_veto: bool = False,
    maturity_score: float = 0.0,
) -> StateDecision:
    f = af.primary
    st = f.structure
    price = f.close
    atr = f.atr14

    # A vetoed asset is never promoted above OBSERVE regardless of its score.
    if hard_veto:
        return StateDecision(
            state=SetupState.OBSERVE if final_score >= cfg.threshold_observe else SetupState.IGNORE,
            rationale="hard veto active (liquidity or safety) — cannot be promoted",
            invalidation="veto must clear before this setup can be acted on",
        )

    if st is None:
        return StateDecision(SetupState.IGNORE, "no structural read available")

    # Structure actively against the setup.
    if st.trend is StructureTrend.DOWNTREND and f.bias is Bias.BEARISH:
        return StateDecision(
            SetupState.INVALIDATED,
            "structural downtrend with bearish primary bias",
            invalidation="requires a higher low and reclaim of EMA20 to re-qualify",
        )
    if st.fake_breakout and final_score < cfg.threshold_watch:
        return StateDecision(
            SetupState.INVALIDATED,
            "breakout attempt failed and score has not recovered",
            invalidation="already invalidated by the failed attempt",
        )

    res = st.nearest_resistance
    d = st.distance_to_resistance_atr

    # -- broken level, price holding above ---------------------------------- #
    if st.broke_out and final_score >= cfg.threshold_watch:
        if st.in_retest:
            return StateDecision(
                SetupState.RETEST,
                "price returned to a level it previously broke and is holding it as support",
                trigger="reclaim and hold above the broken level with volume",
                invalidation=f"a close back below {_lvl(st)} invalidates the retest",
            )
        # Continuation vs fresh breakout: a mature move that is still structurally
        # sound is a continuation, not a new breakout.
        if maturity_score >= 55:
            return StateDecision(
                SetupState.CONTINUATION,
                "move already underway and still structurally intact",
                trigger="continued closes above the broken level on sustained volume",
                invalidation="loss of the broken level or a break of the last higher low",
            )
        return StateDecision(
            SetupState.BREAKOUT,
            "level cleared on a closed candle with the score confirming",
            trigger="already triggered — breakout in progress",
            invalidation=f"a close back below {_lvl(st)} would fail the breakout",
        )

    if st.in_retest and final_score >= cfg.threshold_observe:
        return StateDecision(
            SetupState.RETEST,
            "price testing a previously broken level from above",
            trigger="rejection from the level with rising volume",
            invalidation=f"a close below {_lvl(st)}",
        )

    # -- coiled below a level ------------------------------------------------ #
    if (
        final_score >= cfg.threshold_armed
        and d is not None
        and d <= cfg.armed_max_distance_atr
        and maturity_score < cfg.pump_maturity_max_for_premium
    ):
        trigger = (
            f"a close above {res.price:.8g}"
            + (f" (+{cfg.breakout_confirm_atr:.2f} ATR = {res.price + cfg.breakout_confirm_atr * atr:.8g})" if atr else "")
            + " on sustained volume"
            if res
            else "a close above the mapped resistance on sustained volume"
        )
        return StateDecision(
            SetupState.ARMED,
            f"strong preparatory setup {d:.2f} ATR below its trigger level",
            trigger=trigger,
            invalidation=_invalidation(st, price, atr),
        )

    if final_score >= cfg.threshold_watch:
        return StateDecision(
            SetupState.WATCH,
            "setup forming but not yet in position to trigger",
            trigger="approach to the mapped resistance with volume still rising",
            invalidation=_invalidation(st, price, atr),
        )

    if final_score >= cfg.threshold_observe:
        return StateDecision(SetupState.OBSERVE, "early signs of a behaviour change, not yet actionable")

    return StateDecision(SetupState.IGNORE, "nothing structurally or behaviourally notable")


def _lvl(st) -> str:
    if st.nearest_resistance:
        return f"{st.nearest_resistance.price:.8g}"
    if st.nearest_support:
        return f"{st.nearest_support.price:.8g}"
    return "the level"


def _invalidation(st, price: float, atr: float | None) -> str:
    if st.nearest_support is not None:
        return f"a close below support at {st.nearest_support.price:.8g}"
    if atr:
        return f"a close below {price - 1.5 * atr:.8g} (1.5 ATR under current price)"
    return "loss of the current structure"
