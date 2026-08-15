"""Outcome tracker: resolution correctness, honest non-answers, and analytics."""

from __future__ import annotations

import numpy as np
import pytest

from cryptopulse.backtest.labels import LabelConfig, Outcome
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.core.types import Timeframe
from cryptopulse.outcomes.stats import MIN_SAMPLE, build_performance
from cryptopulse.outcomes.tracker import MAX_FETCH_BARS, OutcomeTracker, PendingSignal
from cryptopulse.providers.fixture import FixtureProvider
from tests.conftest import FIXED_NOW_MS

pytestmark = pytest.mark.asyncio

LABEL = LabelConfig("test_2R", target_atr=2.0, stop_atr=1.0, horizon_bars=10)


def _settings() -> CryptoPulseSettings:
    s = CryptoPulseSettings()
    s.providers.market_data = "fixture"
    s.database.url = "sqlite:///:memory:"
    return s


def _tracker(clock=None, label=LABEL) -> OutcomeTracker:
    clock = clock or FrozenClock(FIXED_NOW_MS)
    return OutcomeTracker(_settings(), FixtureProvider(clock=clock), label_config=label, clock=clock)


# --------------------------------------------------------------------------- #
# The fixture must behave as a stable history for any of this to be meaningful
# --------------------------------------------------------------------------- #


async def test_fixture_candle_is_a_pure_function_of_its_timestamp():
    """Same bar, two different 'now's, identical values — otherwise resolution
    would grade a signal against a different universe than the one it fired in."""
    early = FixtureProvider(clock=FrozenClock(FIXED_NOW_MS))
    later = FixtureProvider(clock=FrozenClock(FIXED_NOW_MS + 6 * 3600 * 1000))

    a = (await early.get_ohlcv("SOLUSDT", Timeframe.M5, 300)).closed()
    b = (await later.get_ohlcv("SOLUSDT", Timeframe.M5, 300)).closed()

    common = np.intersect1d(a.open_time_ms, b.open_time_ms)
    assert common.size > 100, "the two windows should overlap substantially"
    ia = np.searchsorted(a.open_time_ms, common)
    ib = np.searchsorted(b.open_time_ms, common)
    for field in ("open", "high", "low", "close", "volume"):
        np.testing.assert_array_equal(
            getattr(a, field)[ia], getattr(b, field)[ib], err_msg=f"{field} drifted between fetches"
        )


async def test_fixture_history_is_reproducible_across_instances():
    a = FixtureProvider(clock=FrozenClock(FIXED_NOW_MS))
    b = FixtureProvider(clock=FrozenClock(FIXED_NOW_MS))
    s1 = await a.get_ohlcv("BTCUSDT", Timeframe.M5, 200)
    s2 = await b.get_ohlcv("BTCUSDT", Timeframe.M5, 200)
    np.testing.assert_array_equal(s1.close, s2.close)


# --------------------------------------------------------------------------- #
# Timing gates
# --------------------------------------------------------------------------- #


async def test_ready_before_excludes_signals_that_cannot_have_a_verdict_yet():
    clock = FrozenClock(FIXED_NOW_MS)
    tracker = _tracker(clock)
    tf_ms = Timeframe.M5.ms
    # A signal that needs 10 bars of horizon cannot be graded 3 bars later.
    assert tracker.ready_before_ms() < FIXED_NOW_MS - 10 * tf_ms
    assert tracker.ready_before_ms() > FIXED_NOW_MS - 20 * tf_ms


# --------------------------------------------------------------------------- #
# Resolution against a real (synthetic but stable) series
# --------------------------------------------------------------------------- #


async def _series_and_signal(bars_back: int = 40):
    """A signal placed `bars_back` closed bars before now, on the fixture feed."""
    clock = FrozenClock(FIXED_NOW_MS)
    provider = FixtureProvider(clock=clock)
    series = (await provider.get_ohlcv("SOLUSDT", Timeframe.M5, MAX_FETCH_BARS)).closed()
    idx = len(series) - bars_back
    ts = int(series.close_time_ms[idx])
    atr = float(np.mean(series.high[idx - 14 : idx] - series.low[idx - 14 : idx]))
    sig = PendingSignal(id=1, symbol="SOLUSDT", timestamp_ms=ts, price=float(series.close[idx]),
                        atr=atr, timeframe=Timeframe.M5)
    return clock, series, idx, sig


async def test_resolves_a_signal_with_enough_future_bars():
    clock, series, idx, sig = await _series_and_signal(bars_back=40)
    tracker = _tracker(clock)
    report = await tracker.resolve([sig])

    assert report.checked == 1
    assert report.resolved == 1
    assert report.still_pending == 0
    res = report.resolutions[0]
    assert res.label in ("WIN", "LOSS", "TIMEOUT")
    assert res.entry_price is not None and res.entry_price > 0
    assert res.bars_held is not None and 1 <= res.bars_held <= LABEL.horizon_bars
    assert res.label_config == "test_2R"


async def test_resolution_matches_the_label_applied_directly():
    """The tracker must not introduce an off-by-one against label_signal."""
    from cryptopulse.backtest.labels import label_signal

    clock, series, idx, sig = await _series_and_signal(bars_back=40)
    expected = label_signal(series.high, series.low, series.close, series.open, idx, sig.atr, LABEL)

    report = await _tracker(clock).resolve([sig])
    res = report.resolutions[0]
    assert res.label == expected.outcome.value
    assert res.entry_price == pytest.approx(expected.entry_price)
    assert res.return_pct == pytest.approx(expected.return_pct)
    assert res.bars_held == expected.bars_held


async def test_entry_is_the_bar_after_the_signal_not_the_signal_bar():
    clock, series, idx, sig = await _series_and_signal(bars_back=40)
    report = await _tracker(clock).resolve([sig])
    res = report.resolutions[0]
    assert res.entry_price == pytest.approx(float(series.open[idx + 1]))


async def test_signal_too_recent_stays_pending_rather_than_being_settled():
    """A verdict that cannot be known must not be manufactured.

    The barriers are placed far out of reach (huge ATR), so with only a few bars
    available neither can be touched and the horizon has not elapsed — the honest
    answer is "not yet", not a TIMEOUT settled at the current price.
    """
    clock, series, idx, sig = await _series_and_signal(bars_back=3)  # horizon is 10
    sig.atr = float(series.close[idx])  # 1 ATR == the whole price; unreachable
    report = await _tracker(clock).resolve([sig])
    assert report.resolved == 0
    assert report.still_pending == 1
    assert report.resolutions == []


async def test_barrier_hit_early_is_a_verdict_even_before_the_horizon_elapses():
    """Once a barrier is touched the trade is over; waiting out the horizon would
    delay a result that is already known."""
    clock, series, idx, sig = await _series_and_signal(bars_back=3)
    sig.atr = float(series.close[idx]) * 1e-5  # barriers essentially at the price
    report = await _tracker(clock).resolve([sig])
    assert report.resolved == 1
    res = report.resolutions[0]
    assert res.label in ("WIN", "LOSS")
    assert res.bars_held == 1


async def test_signal_without_atr_is_unresolvable_and_says_why():
    clock, series, idx, sig = await _series_and_signal(bars_back=40)
    sig.atr = None
    report = await _tracker(clock).resolve([sig])
    assert report.unresolvable == 1
    res = report.resolutions[0]
    assert res.label == Outcome.UNRESOLVABLE.value
    assert "ATR" in (res.note or "")
    assert res.return_pct is None, "an unresolvable signal must not carry a fabricated return"


async def test_signal_older_than_reachable_history_is_unresolvable():
    clock = FrozenClock(FIXED_NOW_MS)
    ancient = FIXED_NOW_MS - (MAX_FETCH_BARS + 500) * Timeframe.M5.ms
    sig = PendingSignal(id=9, symbol="SOLUSDT", timestamp_ms=ancient, price=100.0, atr=1.0,
                        timeframe=Timeframe.M5)
    report = await _tracker(clock).resolve([sig])
    assert report.unresolvable == 1
    assert "predates" in (report.resolutions[0].note or "")


async def test_timestamp_not_on_a_bar_boundary_is_not_grafted_onto_a_neighbour():
    """An off-by-one bar would shift every barrier, so a near miss is refused."""
    clock, series, idx, sig = await _series_and_signal(bars_back=40)
    sig.timestamp_ms += 1234  # no longer any bar's close time
    report = await _tracker(clock).resolve([sig])
    assert report.unresolvable == 1
    assert "missing" in (report.resolutions[0].note or "")


async def test_provider_failure_is_reported_per_symbol_not_raised():
    clock, series, idx, sig = await _series_and_signal(bars_back=40)
    tracker = _tracker(clock)

    async def broken(*_a, **_k):
        raise RuntimeError("feed down")

    tracker.provider.get_ohlcv = broken
    report = await tracker.resolve([sig])
    assert report.errors and "SOLUSDT" in report.errors
    assert report.resolved == 0


async def test_costs_make_the_net_return_worse_than_gross():
    clock, series, idx, sig = await _series_and_signal(bars_back=40)
    res = (await _tracker(clock).resolve([sig])).resolutions[0]
    assert res.net_return_pct < res.return_pct


async def test_batches_one_fetch_per_symbol():
    clock, series, idx, sig = await _series_and_signal(bars_back=40)
    tracker = _tracker(clock)
    calls = {"n": 0}
    original = tracker.provider.get_ohlcv

    async def counting(symbol, timeframe, limit):
        calls["n"] += 1
        return await original(symbol, timeframe, limit)

    tracker.provider.get_ohlcv = counting
    signals = [
        PendingSignal(id=i, symbol="SOLUSDT", timestamp_ms=int(series.close_time_ms[idx - i]),
                      price=float(series.close[idx - i]), atr=sig.atr, timeframe=Timeframe.M5)
        for i in range(5)
    ]
    await tracker.resolve(signals)
    assert calls["n"] == 1, "five signals on one symbol should cost one fetch"


# --------------------------------------------------------------------------- #
# Performance analytics
# --------------------------------------------------------------------------- #


def _row(outcome="WIN", ret=2.0, score=70.0, state="ARMED", maturity=30.0,
         liq="GOOD", regime="BULL_TREND", symbol="AAAUSDT", components=None, synthetic=False):
    return {
        "symbol": symbol, "outcome": outcome, "return_pct": ret, "net_return_pct": ret - 0.4,
        "final_score": score, "state": state, "pump_maturity": maturity, "liquidity": liq,
        "regime": regime, "mfe_atr": 1.5, "mae_atr": -0.6, "synthetic": synthetic,
        "components": components or {"volume": 12.0, "momentum": 8.0},
    }


async def test_performance_on_no_data_is_empty_not_zero():
    perf = build_performance([]).to_dict()
    assert perf["overall"]["n"] == 0
    assert perf["overall"]["win_rate"] is None, "no data must not read as a 0% win rate"


async def test_performance_computes_rates_with_counts_attached():
    rows = [_row("WIN", 3.0) for _ in range(6)] + [_row("LOSS", -1.5) for _ in range(4)]
    perf = build_performance(rows, use_net=False).to_dict()
    o = perf["overall"]
    assert o["n"] == 10
    assert o["win_rate"] == pytest.approx(0.6)
    assert o["expectancy_pct"] == pytest.approx((6 * 3.0 + 4 * -1.5) / 10)
    assert o["profit_factor"] == pytest.approx(18.0 / 6.0)


async def test_small_buckets_are_flagged_insufficient():
    perf = build_performance([_row() for _ in range(3)]).to_dict()
    assert perf["overall"]["insufficient_sample"] is True
    assert any("Below" in n or "below" in n for n in perf["notes"])


async def test_large_sample_is_not_flagged():
    rows = [_row("WIN" if i % 2 else "LOSS", 2.0 if i % 2 else -1.0) for i in range(MIN_SAMPLE + 5)]
    perf = build_performance(rows).to_dict()
    assert perf["overall"]["insufficient_sample"] is False


async def test_synthetic_signals_are_called_out():
    perf = build_performance([_row(synthetic=True) for _ in range(4)]).to_dict()
    assert perf["synthetic_count"] == 4
    assert any("SYNTHETIC" in n for n in perf["notes"])


async def test_breakdowns_group_correctly():
    rows = [_row(state="ARMED", score=75.0) for _ in range(3)] + [_row(state="BREAKOUT", score=55.0) for _ in range(2)]
    perf = build_performance(rows).to_dict()
    states = {b["key"]: b["n"] for b in perf["by_state"]}
    assert states == {"ARMED": 3, "BREAKOUT": 2}
    bands = {b["key"]: b["n"] for b in perf["by_score_band"]}
    assert bands == {"65-80": 3, "50-65": 2}


async def test_component_edge_separates_winners_from_losers():
    winners = [_row("WIN", 2.0, components={"volume": 18.0, "momentum": 5.0}) for _ in range(5)]
    losers = [_row("LOSS", -1.0, components={"volume": 4.0, "momentum": 5.0}) for _ in range(5)]
    edge = build_performance(winners + losers).to_dict()["component_edge"]
    by_name = {e["component"]: e for e in edge}

    assert by_name["volume"]["edge"] == pytest.approx(14.0), "volume separated the two groups"
    assert by_name["momentum"]["edge"] == pytest.approx(0.0), "momentum scored both groups identically"
    assert edge[0]["component"] == "volume", "the strongest separator should rank first"


async def test_component_edge_needs_both_winners_and_losers():
    assert build_performance([_row("WIN") for _ in range(5)]).to_dict()["component_edge"] == []


# --------------------------------------------------------------------------- #
# The synthetic feed must behave like a market, or label statistics are noise
# --------------------------------------------------------------------------- #


async def test_synthetic_price_path_is_approximately_a_martingale():
    """Guards the Hurst=0.5 scaling in the fixture generator.

    A 2:1 target:stop barrier on a martingale resolves to roughly one third wins.
    An earlier generator produced white noise around a trend, which mean-reverts
    hard and drove random entries to a ~10% win rate — making every synthetic
    outcome look catastrophic for reasons unrelated to any strategy. This test
    fails if that regression returns.
    """
    from cryptopulse.backtest.labels import label_signal
    from cryptopulse.features.indicators import atr as atr_fn

    provider = FixtureProvider(clock=FrozenClock(FIXED_NOW_MS))
    label = LabelConfig("baseline_2R", target_atr=2.0, stop_atr=1.0, horizon_bars=24)
    tally: dict[str, int] = {}

    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "OPUSDT"):
        s = (await provider.get_ohlcv(symbol, Timeframe.M5, 900)).closed()
        a = atr_fn(s.high, s.low, s.close, 14)
        # Entries on a fixed stride, with no scoring involved at all.
        for i in range(200, len(s) - 30, 5):
            if not np.isfinite(a[i]):
                continue
            r = label_signal(s.high, s.low, s.close, s.open, i, float(a[i]), label)
            if r.outcome is Outcome.UNRESOLVED:
                continue
            tally[r.outcome.value] = tally.get(r.outcome.value, 0) + 1

    n = sum(tally.values())
    assert n > 300, "need a decent sample for the rate to be meaningful"
    win_rate = tally.get("WIN", 0) / n
    assert 0.24 <= win_rate <= 0.44, (
        f"random-entry win rate {win_rate:.1%} on a 2:1 barrier. A martingale gives ~33%. "
        "Far below suggests the generator has gone back to mean-reverting noise."
    )
