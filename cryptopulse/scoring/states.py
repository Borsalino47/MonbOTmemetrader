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
from cryptopulse.i18n import num
from cryptopulse.i18n import price as price_fr
from cryptopulse.i18n import reasons as R
from cryptopulse.i18n.labels import SETUP_STATE_FR, SETUP_STATE_LONG_FR

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
            # The identifier stays; the label is what the screen renders. A UI
            # falling back to the value would print "BREAKOUT" in French text.
            "state_label_fr": SETUP_STATE_FR[self.state.value],
            "state_long_fr": SETUP_STATE_LONG_FR[self.state.value],
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
            rationale=R.RAT_VETO(),
            invalidation=R.TRG_VETO(),
        )

    if st is None:
        return StateDecision(SetupState.IGNORE, R.RAT_NO_STRUCTURE())

    # Structure actively against the setup.
    if st.trend is StructureTrend.DOWNTREND and f.bias is Bias.BEARISH:
        return StateDecision(
            SetupState.INVALIDATED,
            R.RAT_DOWNTREND(),
            invalidation=R.TRG_REQUALIFY(),
        )
    if st.fake_breakout and final_score < cfg.threshold_watch:
        return StateDecision(
            SetupState.INVALIDATED,
            R.RAT_FAILED(),
            invalidation=R.TRG_ALREADY_INVALID(),
        )

    res = st.nearest_resistance
    d = st.distance_to_resistance_atr

    # -- broken level, price holding above ---------------------------------- #
    if st.broke_out and final_score >= cfg.threshold_watch:
        if st.in_retest:
            return StateDecision(
                SetupState.RETEST,
                R.RAT_RETEST(),
                trigger=R.TRG_RECLAIM_HOLD(),
                invalidation=R.TRG_CLOSE_BELOW_RETEST(level=_lvl(st)),
            )
        # Continuation vs fresh breakout: a mature move that is still structurally
        # sound is a continuation, not a new breakout.
        if maturity_score >= 55:
            return StateDecision(
                SetupState.CONTINUATION,
                R.RAT_CONTINUATION(),
                trigger=R.TRG_CONTINUED_CLOSES(),
                invalidation=R.TRG_LOSE_LEVEL(),
            )
        return StateDecision(
            SetupState.BREAKOUT,
            R.RAT_BREAKOUT(),
            trigger=R.TRG_IN_PROGRESS(),
            invalidation=R.TRG_CLOSE_BELOW_FAILS(level=_lvl(st)),
        )

    if st.in_retest and final_score >= cfg.threshold_observe:
        return StateDecision(
            SetupState.RETEST,
            R.RAT_RETEST_ABOVE(),
            trigger=R.TRG_REJECTION(),
            invalidation=R.TRG_CLOSE_BELOW(level=_lvl(st)),
        )

    # -- coiled below a level ------------------------------------------------ #
    if (
        final_score >= cfg.threshold_armed
        and d is not None
        and d <= cfg.armed_max_distance_atr
        and maturity_score < cfg.pump_maturity_max_for_premium
    ):
        trigger = (
            R.TRG_CLOSE_ABOVE(level=price_fr(res.price))
            + (
                R.TRG_CONFIRM_ATR_SUFFIX(
                    atr=num(cfg.breakout_confirm_atr),
                    level=price_fr(res.price + cfg.breakout_confirm_atr * atr),
                )
                if atr
                else ""
            )
            + R.TRG_SUSTAINED_VOLUME_SUFFIX()
            if res
            else R.TRG_CLOSE_ABOVE_RESISTANCE()
        )
        return StateDecision(
            SetupState.ARMED,
            R.RAT_ARMED(distance=num(d)),
            trigger=trigger,
            invalidation=_invalidation(st, price, atr),
        )

    if final_score >= cfg.threshold_watch:
        return StateDecision(
            SetupState.WATCH,
            R.RAT_WATCH(),
            trigger=R.TRG_APPROACH(),
            invalidation=_invalidation(st, price, atr),
        )

    if final_score >= cfg.threshold_observe:
        return StateDecision(SetupState.OBSERVE, R.RAT_OBSERVE())

    return StateDecision(SetupState.IGNORE, R.RAT_IGNORE())


def _lvl(st) -> str:
    if st.nearest_resistance:
        return price_fr(st.nearest_resistance.price)
    if st.nearest_support:
        return price_fr(st.nearest_support.price)
    return "the level"


def _invalidation(st, price: float, atr: float | None) -> str:
    if st.nearest_support is not None:
        return R.INV_CLOSE_BELOW_SUPPORT(level=price_fr(st.nearest_support.price))
    if atr:
        return R.INV_CLOSE_BELOW_ATR(level=price_fr(price - 1.5 * atr))
    return "loss of the current structure"
