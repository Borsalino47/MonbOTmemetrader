"""MOONSHOT_ENGINE_V1 — ranking candidates for a *large multiple*, honestly.

--------------------------------------------------------------------------
READ THIS BEFORE YOU TRUST A NUMBER FROM THIS FILE
--------------------------------------------------------------------------

A ten-fold move is a rare event. Across a listed universe of a few dozen assets,
a year passes with none of them, and then two do it in a fortnight. Nothing in
this module changes that base rate, and nothing here has been validated against
one — the outcome tracker has never graded a ×10 label, because no real signal
history exists yet (see CLAUDE.md §3).

So what does it actually do? It measures how closely an asset *currently
resembles the state assets have been in before large expansions*, and ranks on
that. Concretely it answers three separate questions and refuses to blend them
into one comforting number without saying so:

1. **HEADROOM** — has this asset traded ten times higher than it does now,
   inside the history we can see? That is arithmetic on the candles, not a
   forecast. A token 93% below a price it printed last cycle needs the market to
   change its mind, not to invent a new valuation.

2. **CAPACITY** — is a ten-fold move *payable*? Ten times a $20M market cap is
   $200M, which happens somewhere most weeks. Ten times a $40B market cap is
   $400B, which almost nothing has ever reached. This cannot be derived from
   candles at any price, so when no valuation source is configured it is `None`
   and says so — it is never guessed from price, which would be meaningless.

3. **IGNITION** — is the behaviour changing *right now*: volume arriving on a
   base that has been quiet for months, a multi-month level giving way,
   contractions tightening, strength against the market.

The composite score is a weighted blend of the three. **The weights are a
hypothesis, not a fitted model.** They were chosen by reasoning about what
precedes expansions, in exactly the way the opportunity-score weights were, and
they carry the same warning: until real signals have been graded, this ranks
candidates, it does not estimate anything.

A score of 90 does not mean "90% chance of ×10". There is no probability here.
Most of what this module flags will not do ×10. That is a property of the
market, not a defect in the code — and a scanner that implied otherwise would be
lying to you.

--------------------------------------------------------------------------
WHAT IT REFUSES TO DO
--------------------------------------------------------------------------
* It will not score a daily base off a 5-minute chart. Without a timeframe of at
  least 4h it returns UNKNOWN and explains why.
* It will not treat a missing market cap as a small one.
* It will not call a coin that is already up 400% an early entry: high pump
  maturity forces the EXHAUSTION stage and caps the score, because by then the
  move being detected is one you are late to.
* It does not override the liquidity or safety gates. A vetoed asset can carry a
  high moonshot score and still never be alertable — the gates run in
  `ScoreEngine`, above this module, and nothing here can lift them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from cryptopulse.config.settings import MoonshotSettings
from cryptopulse.core.types import Timeframe
from cryptopulse.features.expansion import ExpansionReport
from cryptopulse.features.pipeline import AssetFeatures, Bias, TimeframeFeatures
from cryptopulse.features.stats import clamp01, scale
from cryptopulse.scoring.pump_maturity import PumpMaturity

__all__ = ["MoonshotStage", "MoonshotAssessment", "assess_moonshot", "MOONSHOT_ENGINE_VERSION"]

MOONSHOT_ENGINE_VERSION = "MOONSHOT_ENGINE_V1"

# The reading needs a timeframe slow enough that a multi-week base is visible in
# a few hundred bars. Below this, the answer is "unknown", not a smaller number.
MIN_TIMEFRAME_SECONDS = Timeframe.H4.seconds


class MoonshotStage(str, Enum):
    """Where in the life cycle of a large move this asset appears to be."""

    UNKNOWN = "UNKNOWN"  # not enough history to say
    NEUTRAL = "NEUTRAL"  # nothing here resembles a pre-expansion state
    DORMANT = "DORMANT"  # based and coiled, but nothing arriving yet
    ACCUMULATION = "ACCUMULATION"  # volume building into flat price
    IGNITION = "IGNITION"  # regime shift under way, move still young
    EXPANSION = "EXPANSION"  # markup in progress; not an early entry
    EXHAUSTION = "EXHAUSTION"  # extended and climactic; the entry is gone


# Weights for the ignition sub-signals. A HYPOTHESIS — see the module docstring.
# Level breaks and volume regime carry the most because they are the two things
# essentially every large expansion has in common; the rest are corroboration.
_IGNITION_WEIGHTS: dict[str, float] = {
    "volume_regime": 2.0,
    "level_break": 2.0,
    "accumulation": 1.5,
    "base_maturity": 1.5,
    "trend_reclaim": 1.2,
    "relative_strength": 1.2,
    "compression": 1.0,
    "vcp": 1.0,
    "volume_rank": 1.0,
    "mtf_alignment": 1.0,
    "spring": 0.8,
}


@dataclass(slots=True)
class MoonshotAssessment:
    """Three separate readings and one composite, each with its own provenance."""

    score: float  # 0..100 composite ranking. NOT a probability.
    ignition: float | None  # 0..100 behaviour change happening now
    headroom: float | None  # 0..100 room to a price already printed
    capacity: float | None  # 0..100 room by valuation; None when cap unknown
    stage: MoonshotStage
    timeframe: str | None = None
    target_multiple: float = 10.0
    multiple_to_window_high: float | None = None
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    # What could not be computed. A reader has to be able to see the holes.
    unknowns: list[str] = field(default_factory=list)
    coverage: float = 0.0
    engine_version: str = MOONSHOT_ENGINE_VERSION

    @property
    def is_candidate(self) -> bool:
        """Worth a human's attention, on the two stages where entry is still early."""
        return self.stage in (MoonshotStage.ACCUMULATION, MoonshotStage.IGNITION) and self.score >= 60.0

    def to_dict(self) -> dict:
        return {
            "engine_version": self.engine_version,
            "score": round(self.score, 1),
            "label": f"{self.score:.0f}/100",
            "ignition": None if self.ignition is None else round(self.ignition, 1),
            "headroom": None if self.headroom is None else round(self.headroom, 1),
            "capacity": None if self.capacity is None else round(self.capacity, 1),
            "stage": self.stage.value,
            "timeframe": self.timeframe,
            "target_multiple": self.target_multiple,
            "multiple_to_window_high": (
                None if self.multiple_to_window_high is None else round(self.multiple_to_window_high, 2)
            ),
            "is_candidate": self.is_candidate,
            "coverage": round(self.coverage, 3),
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "reasons": self.reasons,
            "caveats": self.caveats,
            "unknowns": self.unknowns,
            # Worded without the word "probability" on purpose: a test asserts
            # that no score payload contains it anywhere, so that no future edit
            # can slip a likelihood into the one place users read numbers from.
            # The full statement lives in /api/config alongside the V1 one.
            "disclaimer": (
                "Ranking of resemblance to pre-expansion behaviour. Not a likelihood, not calibrated, "
                "and never validated against a graded ×10 outcome."
            ),
        }

    @classmethod
    def unknown(cls, reason: str, target_multiple: float = 10.0) -> MoonshotAssessment:
        return cls(
            score=0.0,
            ignition=None,
            headroom=None,
            capacity=None,
            stage=MoonshotStage.UNKNOWN,
            target_multiple=target_multiple,
            unknowns=[reason],
            coverage=0.0,
        )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def assess_moonshot(
    af: AssetFeatures, maturity: PumpMaturity, cfg: MoonshotSettings
) -> MoonshotAssessment:
    """Score one asset's resemblance to a pre-expansion state."""
    htf = _pick_timeframe(af, cfg)
    if htf is None:
        return MoonshotAssessment.unknown(
            "no timeframe of 4h or slower was available — a multi-week base cannot be assessed "
            "from an intraday chart",
            cfg.target_multiple,
        )
    exp = htf.expansion
    if exp is None or exp.bars < cfg.min_bars:
        have = 0 if exp is None else exp.bars
        return MoonshotAssessment.unknown(
            f"only {have} {htf.timeframe.value} bars of history; {cfg.min_bars} needed to judge a base",
            cfg.target_multiple,
        )

    unknowns: list[str] = []
    reasons: list[str] = []
    caveats: list[str] = []

    headroom = _headroom(exp, cfg, reasons, caveats, unknowns)
    capacity = _capacity(af, cfg, reasons, caveats, unknowns)
    ignition, parts, coverage = _ignition(af, htf, exp, reasons, caveats, unknowns)
    stage = _stage(af, htf, exp, maturity, ignition, cfg)

    score = _composite(ignition, headroom, capacity, cfg)

    # Lateness is not a discount, it is a different situation. Once a move is
    # extended, whatever this module detected is something you are behind, and
    # the score must not keep advertising it as an early candidate.
    if stage is MoonshotStage.EXHAUSTION:
        score = min(score, cfg.exhaustion_score_cap)
        caveats.append(
            f"pump maturity {maturity.score:.0f}/100 — the move is extended; "
            f"score capped at {cfg.exhaustion_score_cap:.0f} because this is not an early entry"
        )
    elif stage is MoonshotStage.EXPANSION:
        score = min(score, cfg.expansion_score_cap)
        caveats.append(
            f"markup already under way; score capped at {cfg.expansion_score_cap:.0f} — "
            "the asymmetry an early entry buys is already spent"
        )

    return MoonshotAssessment(
        score=score,
        ignition=ignition,
        headroom=headroom,
        capacity=capacity,
        stage=stage,
        timeframe=htf.timeframe.value,
        target_multiple=cfg.target_multiple,
        multiple_to_window_high=exp.multiple_to_window_high,
        components=parts,
        reasons=reasons,
        caveats=caveats,
        unknowns=unknowns,
        coverage=coverage,
    )


def _pick_timeframe(af: AssetFeatures, cfg: MoonshotSettings) -> TimeframeFeatures | None:
    """Prefer the configured timeframe; otherwise the slowest one that qualifies."""
    preferred = af.get(cfg.timeframe)
    if preferred is not None:
        return preferred
    slowest = af.highest_timeframe()
    if slowest is None or slowest.timeframe.seconds < MIN_TIMEFRAME_SECONDS:
        return None
    return slowest


# --------------------------------------------------------------------------- #
# 1. Headroom — arithmetic on prices this asset actually printed
# --------------------------------------------------------------------------- #


def _headroom(
    exp: ExpansionReport,
    cfg: MoonshotSettings,
    reasons: list[str],
    caveats: list[str],
    unknowns: list[str],
) -> float | None:
    mult = exp.multiple_to_window_high
    if mult is None or mult <= 0:
        unknowns.append("no window high available — headroom to a prior price cannot be measured")
        return None

    if mult < 1.02:
        # At or above the highest price in the window there is no prior level to
        # return to. That is not bearish; it is simply outside what this measure
        # can see, and pretending otherwise would invent a target.
        caveats.append("trading at the top of its available history — no prior high above to measure against")
        return 0.0

    # Geometric, because the distance from 2x to 4x is the same *kind* of move as
    # 4x to 8x. Saturates at the configured target: beyond it, more is not better.
    import math

    span = math.log(max(cfg.target_multiple, 1.5))
    value = clamp01(math.log(mult) / span) * 100.0

    if mult >= cfg.target_multiple:
        reasons.append(
            f"traded {mult:.1f}x above the current price inside this window — "
            f"a ×{cfg.target_multiple:.0f} return to that level is a price it has actually printed"
        )
    elif mult >= 2.0:
        reasons.append(f"{mult:.1f}x below the highest price in this window")

    if exp.bars_since_window_high is not None and exp.bars_since_window_high > 0:
        age = exp.bars_since_window_high
        if age > 200:
            caveats.append(
                f"that high is {age} bars old — an old high is a reference level, not a magnet"
            )
    if exp.drawdown_from_high_pct is not None and exp.drawdown_from_high_pct < -90:
        caveats.append(
            f"{abs(exp.drawdown_from_high_pct):.0f}% below its window high — assets fall that far for reasons, "
            "and most never return"
        )
    return value


# --------------------------------------------------------------------------- #
# 2. Capacity — can a ×N actually be paid for?
# --------------------------------------------------------------------------- #


def _capacity(
    af: AssetFeatures,
    cfg: MoonshotSettings,
    reasons: list[str],
    caveats: list[str],
    unknowns: list[str],
) -> float | None:
    val = af.valuation
    if val is None:
        unknowns.append(
            "no valuation source configured — market cap unknown, so whether a "
            f"×{cfg.target_multiple:.0f} is payable cannot be judged (set CP_PROVIDER_VALUATION)"
        )
        return None

    import math

    cap = val.market_cap_usd
    bounded = False
    if cap is None:
        cap = val.market_cap_upper_bound_usd
        bounded = True
    if cap is None or cap <= 0:
        unknowns.append(f"{val.symbol}: valuation source returned no market cap")
        return None

    # Log-scaled between the two ends of the argument: at the small end a ×N is
    # an ordinary week somewhere in the market; at the large end it would make
    # the asset one of the biggest that has ever existed.
    lo, hi = math.log10(max(cfg.cap_full_capacity_usd, 1.0)), math.log10(max(cfg.cap_no_capacity_usd, 10.0))
    value = (1.0 - clamp01((math.log10(cap) - lo) / (hi - lo))) * 100.0

    implied = cap * cfg.target_multiple
    if bounded:
        # cap <= bound, and capacity falls as cap rises, so the figure computed
        # from the bound is a floor on the true capacity. Stated as a floor.
        reasons.append(
            f"outside the ranked valuation set: market cap is below {cap:,.0f} USD, "
            f"so capacity is at least {value:.0f}/100"
        )
    else:
        reasons.append(
            f"market cap {cap:,.0f} USD — a ×{cfg.target_multiple:.0f} implies {implied:,.0f} USD"
        )
        if value < 25:
            caveats.append(
                f"a ×{cfg.target_multiple:.0f} from here implies {implied:,.0f} USD, a valuation "
                "very few assets have ever reached"
            )

    ratio = val.circulating_ratio
    if ratio is not None and ratio < 0.5:
        # Tokens still to unlock are future supply competing with your exit.
        value *= 0.8
        caveats.append(
            f"only {ratio:.0%} of supply circulating — unlocks add sell pressure the price chart cannot show"
        )
    if val.ambiguous_symbol:
        caveats.append(
            f"ticker {val.symbol} matched more than one asset in the valuation source; "
            "the largest was used and may be the wrong one"
        )
    return clamp01(value / 100.0) * 100.0


# --------------------------------------------------------------------------- #
# 3. Ignition — is the behaviour changing right now?
# --------------------------------------------------------------------------- #


def _ignition(
    af: AssetFeatures,
    htf: TimeframeFeatures,
    exp: ExpansionReport,
    reasons: list[str],
    caveats: list[str],
    unknowns: list[str],
) -> tuple[float, dict[str, float], float]:
    parts: dict[str, float] = {}
    tf = htf.timeframe.value

    # -- volume regime: is money arriving that was not here last month? ------ #
    if exp.volume_regime_ratio is not None:
        # 1.5x is a busy week; 5x on a daily chart against a 30-bar median is the
        # kind of participation change that precedes a markup.
        parts["volume_regime"] = scale(exp.volume_regime_ratio, 1.5, 5.0)
        if exp.volume_regime_ratio >= 2.5:
            reasons.append(
                f"{tf} volume {exp.volume_regime_ratio:.1f}x its own 30-bar median — a regime shift, "
                "not a busy bar"
            )
        elif exp.volume_regime_ratio < 1.0:
            caveats.append(f"{tf} volume below its recent median — nothing is arriving yet")
    else:
        unknowns.append(f"{tf} volume regime unavailable (needs 35 bars)")

    # -- the same volume, ranked against every other asset scanned ----------- #
    if af.rvol_percentile_universe is not None:
        parts["volume_rank"] = af.rvol_percentile_universe
        if af.rvol_percentile_universe >= 0.9:
            reasons.append(
                f"relative volume above {af.rvol_percentile_universe:.0%} of the scanned universe — "
                "busy against the market, not just against itself"
            )
    else:
        unknowns.append("cross-sectional volume rank not computed for this scan")

    # -- accumulation: buying into a price that will not move ---------------- #
    acc_parts: list[float] = []
    if exp.cmf is not None:
        acc_parts.append(scale(exp.cmf, 0.0, 0.25))
    if exp.ad_slope_norm is not None:
        acc_parts.append(scale(exp.ad_slope_norm, 0.0, 0.05))
    if exp.quiet_accumulation:
        acc_parts.append(1.0)
        reasons.append(
            f"{tf} quiet accumulation: volume building, price flat "
            f"({exp.price_drift_pct:+.1f}% over 20 bars), closes in the upper half of their ranges"
        )
    if acc_parts:
        parts["accumulation"] = sum(acc_parts) / len(acc_parts)
    else:
        unknowns.append(f"{tf} accumulation tape unavailable")

    # -- how long has it been building? -------------------------------------- #
    if exp.base_length_bars is not None:
        parts["base_maturity"] = scale(float(exp.base_length_bars), 10.0, 120.0)
        if exp.base_length_bars >= 40:
            reasons.append(f"{exp.base_length_bars} {tf} bars inside one range — a long base to release from")
    else:
        unknowns.append(f"{tf} base length unavailable (no ATR)")

    # -- coiling -------------------------------------------------------------- #
    if htf.compression is not None:
        parts["compression"] = 1.0 - htf.compression
        if htf.compression <= 0.2:
            reasons.append(f"{tf} volatility in the tightest {htf.compression * 100:.0f}% of its recent range")

    # -- the level giving way -------------------------------------------------- #
    level = 0.0
    if exp.broke_prior_high:
        level = 1.0
        reasons.append(
            f"closed above its {exp.prior_high_lookback}-bar {tf} high "
            f"({exp.prior_high:.8g}) — a multi-month level has given way"
        )
    elif exp.broke_base_high:
        level = 0.6
        reasons.append(f"broke the top of its {tf} base ({exp.base_high:.8g})")
    elif exp.base_high is not None and htf.atr14:
        # Not broken yet: credit proximity, because the point of this scanner is
        # to be early rather than to confirm.
        distance_atr = (exp.base_high - htf.close) / htf.atr14
        if distance_atr >= 0:
            level = 0.45 * (1.0 - scale(distance_atr, 0.0, 3.0))
    parts["level_break"] = level

    # -- reclaiming the trend after a long decline ---------------------------- #
    if htf.ema50 is not None:
        reclaim = 0.0
        if htf.close > htf.ema50:
            reclaim += 0.6
        if htf.ema20 is not None and htf.ema20 > htf.ema50:
            reclaim += 0.4
            if htf.close > htf.ema50:
                reasons.append(f"{tf} price above EMA50 with EMA20 crossed up — the downtrend has been reclaimed")
        parts["trend_reclaim"] = reclaim
    else:
        unknowns.append(f"{tf} EMA50 unavailable (needs 50 bars)")

    # -- strength against the market itself ----------------------------------- #
    if af.rs_vs_benchmark_pct is not None:
        parts["relative_strength"] = scale(af.rs_vs_benchmark_pct, 0.0, 25.0)
        if af.rs_vs_benchmark_pct >= 10:
            reasons.append(
                f"outperforming {af.benchmark_symbol or 'the benchmark'} by "
                f"{af.rs_vs_benchmark_pct:.0f} points over the same window"
            )
        elif af.rs_vs_benchmark_pct <= -10:
            caveats.append(
                f"lagging {af.benchmark_symbol or 'the benchmark'} by {abs(af.rs_vs_benchmark_pct):.0f} points"
            )
    else:
        unknowns.append("relative strength vs the benchmark not computed for this scan")

    # -- contraction pattern and spring ---------------------------------------- #
    if exp.contractions:
        parts["vcp"] = 1.0 if exp.vcp else scale(float(len(exp.contractions)), 1.0, 4.0) * 0.4
        if exp.vcp:
            depths = " → ".join(f"{d:.0f}%" for d in exp.contractions)
            reasons.append(f"{tf} pullbacks tightening ({depths}) — supply drying up")
    parts["spring"] = 1.0 if exp.spring else 0.0
    if exp.spring:
        reasons.append(f"{tf} failed breakdown: price lost the base floor and closed back above it")

    # -- do the faster timeframes agree? --------------------------------------- #
    known = [f for f in af.timeframes.values() if f.bias is not Bias.UNKNOWN]
    if len(known) >= 2:
        bullish = sum(1 for f in known if f.bias is Bias.BULLISH)
        parts["mtf_alignment"] = bullish / len(known)

    if not parts:
        return 0.0, {}, 0.0

    total_w = sum(_IGNITION_WEIGHTS[k] for k in parts if k in _IGNITION_WEIGHTS)
    weighted = sum(_IGNITION_WEIGHTS[k] * v for k, v in parts.items() if k in _IGNITION_WEIGHTS)
    ignition = 100.0 * clamp01(weighted / total_w) if total_w else 0.0
    coverage = total_w / sum(_IGNITION_WEIGHTS.values())
    return ignition, parts, coverage


# --------------------------------------------------------------------------- #
# Stage and composite
# --------------------------------------------------------------------------- #


def _stage(
    af: AssetFeatures,
    htf: TimeframeFeatures,
    exp: ExpansionReport,
    maturity: PumpMaturity,
    ignition: float,
    cfg: MoonshotSettings,
) -> MoonshotStage:
    """Order matters: lateness is checked first, because it overrides everything."""
    if maturity.score >= cfg.exhaustion_maturity or htf.volume_climax:
        return MoonshotStage.EXHAUSTION

    # "Already running" is measured on the same timeframe the base was measured
    # on, so a 60% daily run and a 60% five-minute run are not confused. Two
    # measures, because a fast vertical run and a slow grind away from the mean
    # are both markups and only one of them shows up in a 12-bar return.
    fast = htf.roc12 is not None and htf.roc12 >= cfg.expansion_roc_pct
    extended = (
        htf.ema50 is not None
        and htf.atr14
        and (htf.close - htf.ema50) / htf.atr14 >= cfg.expansion_extension_atr
    )
    if (fast or extended) and (exp.broke_prior_high or exp.broke_base_high):
        return MoonshotStage.EXPANSION

    igniting = ignition >= cfg.ignition_threshold and (
        exp.broke_prior_high
        or exp.broke_base_high
        or (exp.volume_regime_ratio is not None and exp.volume_regime_ratio >= 2.0)
    )
    if igniting:
        return MoonshotStage.IGNITION

    based = (exp.base_length_bars or 0) >= cfg.min_base_bars
    if based and (exp.quiet_accumulation or (htf.compression is not None and htf.compression <= 0.4)):
        return MoonshotStage.ACCUMULATION
    if based:
        # Coiled but nothing arriving. Worth a watchlist, not an alert — which is
        # why DORMANT is not one of the stages `evaluate_moonshots` fires on.
        return MoonshotStage.DORMANT

    # No base, no ignition. An asset can be in a perfectly good short-term trend
    # and land here: this stage says "not a ×10 shape", not "not moving".
    return MoonshotStage.NEUTRAL


def _composite(
    ignition: float | None, headroom: float | None, capacity: float | None, cfg: MoonshotSettings
) -> float:
    """Weighted blend over whatever is available, renormalised — never zero-filled.

    A missing capacity must not drag the score down as if capacity were zero;
    it redistributes onto the readings that do exist, and `unknowns` records
    that the blend was computed on partial information.
    """
    pairs = [
        (ignition, cfg.w_ignition),
        (headroom, cfg.w_headroom),
        (capacity, cfg.w_capacity),
    ]
    available = [(v, w) for v, w in pairs if v is not None]
    if not available:
        return 0.0
    total_w = sum(w for _, w in available)
    if total_w <= 0:
        return 0.0
    return sum(v * w for v, w in available) / total_w
