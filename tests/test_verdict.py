"""The four-level verdict: does the compression preserve the warnings?

The verdict is the only part of the product a non-specialist will read, which
makes it the easiest place to accidentally promise something. These tests assert
the ordering of its rules (a hard gate always wins), that 🟢 requires every
condition rather than a good score alone, and that no verdict ever ships without
its caveat.

The stubs below carry exactly the attributes `build_verdict` reads. A companion
test in `test_scoring.py` runs the real engine end to end, so a drift between
this shape and `ScoreResult` cannot pass unnoticed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryptopulse.risk.liquidity import LiquidityStatus
from cryptopulse.scoring.states import SetupState
from cryptopulse.scoring.verdict import VerdictLevel, build_verdict


@dataclass
class _Maturity:
    score: float = 20.0
    is_late: bool = False


@dataclass
class _Confidence:
    score: float = 85.0
    issues: list[str] = field(default_factory=list)


@dataclass
class _Liquidity:
    status: LiquidityStatus = LiquidityStatus.GOOD
    veto: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass
class _Safety:
    score: float = 80.0
    hard_veto: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass
class _State:
    state: SetupState = SetupState.ARMED
    rationale: str = "price is coiling under resistance"
    trigger: str | None = None
    invalidation: str | None = None


@dataclass
class _Penalty:
    reason: str


@dataclass
class _Penalties:
    items: list[_Penalty] = field(default_factory=list)


@dataclass
class _Result:
    final_score: float = 78.0
    risk_penalty: float = 2.0
    maturity: _Maturity = field(default_factory=_Maturity)
    confidence: _Confidence = field(default_factory=_Confidence)
    liquidity: _Liquidity = field(default_factory=_Liquidity)
    safety: _Safety = field(default_factory=_Safety)
    state: _State = field(default_factory=_State)
    penalties: _Penalties = field(default_factory=_Penalties)
    premium: bool = True

    @property
    def is_premium(self) -> bool:
        return self.premium


# --------------------------------------------------------------------------- #
# The disqualifying conditions come first, whatever the score says
# --------------------------------------------------------------------------- #


def test_a_liquidity_veto_is_avoid_even_on_a_perfect_score():
    r = _Result(final_score=100.0, liquidity=_Liquidity(veto=True, reasons=["spread is 400 bps"]))
    v = build_verdict(r)
    assert v.level is VerdictLevel.AVOID
    assert v.emoji == "🔴"
    assert "spread is 400 bps" in v.reasons


def test_a_safety_veto_is_avoid_even_on_a_perfect_score():
    r = _Result(final_score=100.0, safety=_Safety(hard_veto=True, reasons=["listed under 48 hours ago"]))
    assert build_verdict(r).level is VerdictLevel.AVOID


def test_untrustworthy_data_is_avoid_rather_than_a_confident_rating():
    r = _Result(final_score=90.0, confidence=_Confidence(score=20.0, issues=["data is 4 hours old"]))
    v = build_verdict(r)
    assert v.level is VerdictLevel.AVOID
    assert "data is 4 hours old" in v.reasons
    assert "not reliable enough" in v.headline


def test_a_gate_failure_outranks_a_late_move():
    """Both conditions hold; the harder one must be the one reported."""
    r = _Result(maturity=_Maturity(score=95.0, is_late=True), liquidity=_Liquidity(veto=True))
    assert build_verdict(r).level is VerdictLevel.AVOID


# --------------------------------------------------------------------------- #
# 🟠 real signal, bad entry
# --------------------------------------------------------------------------- #


def test_a_late_move_is_risky_not_strong():
    r = _Result(final_score=88.0, maturity=_Maturity(score=85.0, is_late=True))
    v = build_verdict(r)
    assert v.level is VerdictLevel.RISKY
    assert v.emoji == "🟠"
    assert "already advanced" in v.headline


def test_a_heavy_risk_penalty_is_risky_and_names_the_deductions():
    r = _Result(risk_penalty=30.0, penalties=_Penalties([_Penalty("RSI above 80"), _Penalty("thin book")]))
    v = build_verdict(r)
    assert v.level is VerdictLevel.RISKY
    assert "RSI above 80" in v.reasons


# --------------------------------------------------------------------------- #
# 🟢 requires all four conditions
# --------------------------------------------------------------------------- #


def test_strong_requires_premium_and_a_triggered_setup():
    v = build_verdict(_Result())
    assert v.level is VerdictLevel.STRONG
    assert v.emoji == "🟢"
    assert v.label_fr == "FORTE OPPORTUNITÉ"


def test_a_high_score_alone_is_not_strong():
    """Premium is the conjunction of score, gates, confidence and state. A row
    that scores well but fails any of them must not get the green badge."""
    r = _Result(final_score=95.0, premium=False)
    assert build_verdict(r).level is not VerdictLevel.STRONG


def test_a_premium_row_that_has_not_triggered_is_only_watch():
    r = _Result(state=_State(state=SetupState.OBSERVE, trigger="a close above 102.4"))
    v = build_verdict(r)
    assert v.level is VerdictLevel.WATCH
    assert v.emoji == "🟡"
    assert any("a close above 102.4" in x for x in v.reasons)


# --------------------------------------------------------------------------- #
# 🟡 the quiet default
# --------------------------------------------------------------------------- #


def test_a_dormant_asset_is_watch_and_says_nothing_is_happening():
    r = _Result(final_score=12.0, premium=False, state=_State(state=SetupState.IGNORE))
    v = build_verdict(r)
    assert v.level is VerdictLevel.WATCH
    assert "Nothing is happening" in v.headline


# --------------------------------------------------------------------------- #
# Universal properties
# --------------------------------------------------------------------------- #


def _every_branch() -> list[_Result]:
    return [
        _Result(),
        _Result(premium=False, state=_State(state=SetupState.WATCH)),
        _Result(maturity=_Maturity(score=90.0, is_late=True)),
        _Result(risk_penalty=35.0),
        _Result(liquidity=_Liquidity(veto=True)),
        _Result(safety=_Safety(hard_veto=True)),
        _Result(confidence=_Confidence(score=10.0)),
        _Result(final_score=5.0, premium=False, state=_State(state=SetupState.IGNORE)),
    ]


def test_every_verdict_carries_its_caveat_including_the_good_ones():
    for r in _every_branch():
        d = build_verdict(r).to_dict()
        assert "not a probability" in d["caveat"]
        assert "probabilité" in d["caveat_fr"]


def test_every_verdict_explains_itself_in_both_languages():
    for r in _every_branch():
        v = build_verdict(r)
        assert v.headline and v.headline_fr
        assert v.reasons, "a verdict with no reason is a bare badge"
        assert len(v.reasons) == len(v.reasons_fr)


def test_the_four_levels_each_have_a_distinct_emoji_and_french_label():
    seen = {build_verdict(r).level: build_verdict(r) for r in _every_branch()}
    assert set(seen) == set(VerdictLevel)
    assert len({v.emoji for v in seen.values()}) == 4
    assert len({v.label_fr for v in seen.values()}) == 4


def test_a_triggered_setup_below_the_bar_is_not_described_as_untriggered():
    """A BREAKOUT row scoring 53 is watch-level, but saying 'has not triggered'
    would contradict the state shown right beside it in the table."""
    r = _Result(final_score=53.0, premium=False, state=_State(state=SetupState.BREAKOUT))
    v = build_verdict(r)
    assert v.level is VerdictLevel.WATCH
    assert "has not triggered" not in v.headline
    assert "has triggered" in v.headline
    assert any("below the 72 needed" in x for x in v.reasons), "it must say what is missing"


def test_a_triggered_setup_says_which_condition_it_fails():
    r = _Result(
        final_score=90.0, premium=False,
        confidence=_Confidence(score=55.0), state=_State(state=SetupState.BREAKOUT),
    )
    joined = " ".join(build_verdict(r).reasons)
    assert "data confidence is 55" in joined
    assert "below the 72 needed" not in joined, "the score is fine; only confidence is not"
