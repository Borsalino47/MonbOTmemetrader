"""TOKEN_DISCOVERY_SCORE and the deep scan.

The discovery score exists only if it answers a *different* question from the
opportunity score. So the first tests here are about divergence: a token that
scores well on one must be able to score badly on the other, and specifically in
the two directions that motivated splitting them — a large-cap in a clean setup
(good opportunity, nothing has changed) and a quiet token whose volume has
tripled while its price sits still (good discovery, no setup yet).

The rest hold the same lines as everywhere else in this project: an unavailable
input scores zero *and says so*, a score is never a percentage, and the weights
are versioned so today's rows cannot be reinterpreted under tomorrow's rules.
"""

from __future__ import annotations

import numpy as np
import pytest

from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.core.types import Timeframe
from cryptopulse.features.pipeline import AssetFeatures, TimeframeFeatures
from cryptopulse.hunter.deep import DeepScanner
from cryptopulse.hunter.discovery import Candidate
from cryptopulse.risk.liquidity import LiquidityStatus
from cryptopulse.scanner.cex import CexScanner
from cryptopulse.scanner.memory import ScoreMemory
from cryptopulse.scoring.discovery import (
    DISCOVERY_ENGINE_VERSION,
    WEIGHTS,
    DiscoveryEngine,
)
from cryptopulse.scoring.engine import ScoreEngine
from tests.conftest import FIXED_NOW_MS, make_series


def _asset(closes, volumes=None, *, symbol="TESTUSDT", quote_volume=50_000_000.0) -> AssetFeatures:
    tfs = (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)
    per_tf = {
        tf: TimeframeFeatures.build(make_series(closes, timeframe=tf, volumes=volumes), min_bars=60)
        for tf in tfs
    }
    return AssetFeatures(
        symbol=symbol,
        primary_timeframe=Timeframe.M5,
        timeframes=per_tf,
        quote_volume_24h=quote_volume,
    )


def _coiled(n=300, price=100.0):
    """A price that has gone nowhere: tight range, no move to be late to."""
    rng = np.random.default_rng(7)
    return price * np.exp(np.cumsum(rng.normal(0.0, 0.0006, n)))


def _already_ran(n=300, price=100.0):
    """A price that has already made its move."""
    base = np.exp(np.cumsum(np.random.default_rng(3).normal(0.0, 0.001, n)))
    ramp = np.linspace(1.0, 1.45, n)
    return price * base * ramp


def _volume_surge(n=300, base=1000.0, factor=4.0):
    """Normal volume for most of the window, then a genuine surge at the end."""
    v = np.full(n, base)
    v[-6:] = base * factor
    return v


def _engine() -> DiscoveryEngine:
    return DiscoveryEngine(CryptoPulseSettings())


# --------------------------------------------------------------------------- #
# The engine contract
# --------------------------------------------------------------------------- #


def test_the_weights_sum_to_one_hundred():
    assert sum(WEIGHTS.values()) == pytest.approx(100.0)


def test_the_score_is_versioned_and_fingerprinted():
    """A weight change must make old rows unmistakably old, or a future analysis
    would pool scores produced by two different engines."""
    e = _engine()
    assert e.weights_fingerprint
    assert len(e.weights_fingerprint) == 12

    original = WEIGHTS["anomaly"]
    try:
        WEIGHTS["anomaly"] = original + 5.0
        WEIGHTS["tradeability"] -= 5.0
        assert DiscoveryEngine(CryptoPulseSettings()).weights_fingerprint != e.weights_fingerprint
    finally:
        WEIGHTS["anomaly"] = original
        WEIGHTS["tradeability"] += 5.0


def test_the_engine_refuses_weights_that_do_not_sum_to_a_hundred():
    original = WEIGHTS["anomaly"]
    try:
        WEIGHTS["anomaly"] = original + 10.0
        with pytest.raises(ValueError, match="must sum to 100"):
            DiscoveryEngine(CryptoPulseSettings())
    finally:
        WEIGHTS["anomaly"] = original


def test_the_score_stays_on_a_zero_to_hundred_scale():
    e = _engine()
    r = e.score(
        _asset(_coiled(), _volume_surge(factor=50.0)),
        timestamp_ms=FIXED_NOW_MS,
        volume_excess_vs_yesterday=900.0,
        maturity_score=0.0,
        liquidity_status=LiquidityStatus.EXCELLENT,
        range_position=0.0,
    )
    assert 0.0 <= r.score <= 100.0


# --------------------------------------------------------------------------- #
# The divergence that justifies a second engine
# --------------------------------------------------------------------------- #


def test_a_quiet_token_whose_volume_surged_scores_higher_on_discovery():
    """Volume tripled, price has not moved. There is no setup yet — which is the
    entire point of finding it early."""
    af = _asset(_coiled(), _volume_surge(factor=5.0), quote_volume=8_000_000.0)

    discovery = _engine().score(
        af, timestamp_ms=FIXED_NOW_MS, volume_excess_vs_yesterday=9.0,
        maturity_score=5.0, liquidity_status=LiquidityStatus.GOOD, range_position=0.2,
    )
    opportunity = ScoreEngine(CryptoPulseSettings()).score(af, FIXED_NOW_MS)

    assert discovery.score > opportunity.final_score, (
        f"discovery {discovery.score:.1f} should exceed opportunity "
        f"{opportunity.final_score:.1f} on a pre-setup token"
    )
    assert discovery.why(), "a score above zero must explain itself"


def test_a_token_that_already_ran_cannot_score_well_on_discovery():
    """However loud it is. Discovery is about a change that has just begun."""
    af = _asset(_already_ran(), _volume_surge(factor=6.0))
    r = _engine().score(
        af, timestamp_ms=FIXED_NOW_MS, volume_excess_vs_yesterday=15.0,
        maturity_score=88.0, liquidity_status=LiquidityStatus.EXCELLENT, range_position=0.97,
    )
    assert r.score < 60.0, f"a spent move scored {r.score:.1f} on discovery"
    assert any("already advanced" in x or "24h range" in x for x in r.risks())


def test_maturity_and_range_both_have_to_be_early():
    early = _engine().score(
        _asset(_coiled(), _volume_surge()), timestamp_ms=FIXED_NOW_MS,
        maturity_score=10.0, liquidity_status=LiquidityStatus.GOOD, range_position=0.15,
    )
    late = _engine().score(
        _asset(_coiled(), _volume_surge()), timestamp_ms=FIXED_NOW_MS,
        maturity_score=85.0, liquidity_status=LiquidityStatus.GOOD, range_position=0.95,
    )
    assert early.score > late.score


# --------------------------------------------------------------------------- #
# Missing inputs are stated, never filled in
# --------------------------------------------------------------------------- #


def test_an_unsupplied_input_scores_zero_and_says_so():
    r = _engine().score(
        _asset(_coiled(), _volume_surge()),
        timestamp_ms=FIXED_NOW_MS,
        volume_excess_vs_yesterday=None,
        maturity_score=None,
        liquidity_status=None,
        range_position=None,
    )
    by = {c.name: c for c in r.components}
    assert by["earliness"].available is False
    assert by["tradeability"].available is False
    assert any("DATA_UNAVAILABLE" in x for x in by["earliness"].caveats)
    assert any("DATA_UNAVAILABLE" in x for x in by["tradeability"].caveats)
    assert by["earliness"].points == 0.0


def test_every_component_that_scores_explains_why():
    r = _engine().score(
        _asset(_coiled(), _volume_surge()), timestamp_ms=FIXED_NOW_MS,
        volume_excess_vs_yesterday=6.0, maturity_score=12.0,
        liquidity_status=LiquidityStatus.GOOD, range_position=0.25,
    )
    for c in r.components:
        if c.points > 0:
            assert c.reasons, f"{c.name} awarded {c.points:.1f} points with no reason"


def test_no_component_can_exceed_its_weight():
    r = _engine().score(
        _asset(_coiled(), _volume_surge(factor=40.0)), timestamp_ms=FIXED_NOW_MS,
        volume_excess_vs_yesterday=500.0, maturity_score=0.0,
        liquidity_status=LiquidityStatus.EXCELLENT, range_position=0.0,
    )
    for c in r.components:
        assert c.points <= c.max_points + 1e-9, f"{c.name} exceeded its weight"


def test_the_discovery_score_is_never_presented_as_a_probability():
    d = _engine().score(
        _asset(_coiled(), _volume_surge()), timestamp_ms=FIXED_NOW_MS,
        maturity_score=10.0, liquidity_status=LiquidityStatus.GOOD, range_position=0.3,
    ).to_dict()

    assert d["discovery_label"].endswith("/100")
    assert "not a probability" in d["disclaimer"]
    assert d["engine_version"] == DISCOVERY_ENGINE_VERSION
    assert d["weights_fingerprint"]


# --------------------------------------------------------------------------- #
# The deep scan
# --------------------------------------------------------------------------- #


def _candidate(symbol: str, **kw) -> Candidate:
    return Candidate(
        symbol=symbol, price=kw.get("price", 100.0),
        quote_volume_24h=kw.get("volume", 5_000_000.0),
        change_pct_24h=kw.get("change", 1.0),
        range_position=kw.get("range_position", 0.3),
        spread_bps=kw.get("spread", 12.0),
        trades_24h=kw.get("trades", 20_000),
        avg_trade_size=250.0,
        volume_excess_vs_yesterday=kw.get("excess"),
    )


def _scanner(settings: CryptoPulseSettings) -> CexScanner:
    from cryptopulse.providers.fixture import FixtureProvider

    clock = FrozenClock(FIXED_NOW_MS)
    return CexScanner(settings, provider=FixtureProvider(clock=clock), memory=ScoreMemory(), clock=clock)


def _settings() -> CryptoPulseSettings:
    s = CryptoPulseSettings()
    s.providers.market_data = "fixture"
    s.scanner.min_quote_volume_24h = 100_000.0
    return s


async def test_the_deep_scan_reuses_what_the_classic_scan_already_analysed():
    """Paying twice for the same candles is the easiest cost to avoid, and the
    easiest to miss."""
    settings = _settings()
    scanner = _scanner(settings)
    report = await scanner.scan()
    covered = [r.symbol for r in report.results[:5]]

    deep = DeepScanner(settings, scanner, clock=FrozenClock(FIXED_NOW_MS))
    result = await deep.run([_candidate(s) for s in covered])

    assert result.reused == len(covered)
    assert result.fetched == 0
    assert result.kline_requests == 0, "a fully reused deep scan must cost nothing"
    assert len(result.results) == len(covered)


async def test_the_deep_scan_states_what_it_spent_on_new_symbols():
    settings = _settings()
    scanner = _scanner(settings)
    await scanner.scan()

    deep = DeepScanner(settings, scanner, clock=FrozenClock(FIXED_NOW_MS))
    # A symbol the fixture knows but the scan did not cover.
    result = await deep.run([_candidate("SEIUSDT"), _candidate("PEPEUSDT")])

    total = result.reused + result.fetched
    assert total == 2
    assert result.kline_requests == result.fetched * len(settings.scanner.timeframes)


async def test_the_budget_is_enforced_before_the_work_starts():
    settings = _settings()
    scanner = _scanner(settings)
    await scanner.scan()

    deep = DeepScanner(settings, scanner, clock=FrozenClock(FIXED_NOW_MS))
    result = await deep.run([_candidate(f"T{i}USDT") for i in range(30)], max_symbols=4)

    assert result.examined == 4
    assert any("not analysed rather than analysed badly" in n for n in result.notes)


async def test_one_failing_symbol_does_not_fail_the_search():
    settings = _settings()
    scanner = _scanner(settings)
    report = await scanner.scan()
    good = report.results[0].symbol

    deep = DeepScanner(settings, scanner, clock=FrozenClock(FIXED_NOW_MS))
    result = await deep.run([_candidate(good), _candidate("NOSUCHTOKENUSDT")])

    assert "NOSUCHTOKENUSDT" in result.errors
    assert len(result.results) == 1
    assert result.results[0].discovery.symbol == good


async def test_results_are_ranked_by_discovery_not_by_opportunity():
    settings = _settings()
    scanner = _scanner(settings)
    report = await scanner.scan()

    deep = DeepScanner(settings, scanner, clock=FrozenClock(FIXED_NOW_MS))
    result = await deep.run([_candidate(r.symbol) for r in report.results[:8]])

    scores = [r.discovery.score for r in result.results]
    assert scores == sorted(scores, reverse=True)


async def test_both_scores_are_shown_side_by_side_and_never_blended():
    """Averaging them would hide which of the two carried any signal."""
    settings = _settings()
    scanner = _scanner(settings)
    report = await scanner.scan()

    deep = DeepScanner(settings, scanner, clock=FrozenClock(FIXED_NOW_MS))
    result = await deep.run([_candidate(report.results[0].symbol)])
    d = result.results[0].to_dict()

    assert "discovery" in d and "opportunity" in d
    assert d["discovery"]["discovery_label"].endswith("/100")
    assert d["opportunity"]["opportunity_label"].endswith("/100")
    assert "combined" not in d and "blended" not in d


async def test_an_empty_candidate_list_is_not_an_error():
    settings = _settings()
    scanner = _scanner(settings)
    deep = DeepScanner(settings, scanner, clock=FrozenClock(FIXED_NOW_MS))
    result = await deep.run([])
    assert result.examined == 0
    assert result.results == []
    assert any("no candidates" in n for n in result.notes)
