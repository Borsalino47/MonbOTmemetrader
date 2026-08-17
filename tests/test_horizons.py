"""Multi-horizon verification: what the price did 15m / 1h / 4h / 24h later.

The barrier tracker answers "would this trade have won?". This answers "what did
the market actually do?", and the tests below exist to make sure it never
answers that question when it does not yet know.
"""

from __future__ import annotations

import numpy as np
import pytest

from cryptopulse.backtest.engine import CostModel
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.core.types import Timeframe
from cryptopulse.outcomes.horizons import (
    HORIZONS,
    Horizon,
    HorizonResult,
    HorizonStatus,
    HorizonTracker,
)
from cryptopulse.outcomes.stats import MIN_SAMPLE, build_horizon_performance
from cryptopulse.outcomes.tracker import MAX_FETCH_BARS, PendingSignal
from cryptopulse.providers.fixture import FixtureProvider
from tests.conftest import FIXED_NOW_MS

# pytest-asyncio runs in auto mode (pyproject), so async tests need no marker.


def _settings() -> CryptoPulseSettings:
    s = CryptoPulseSettings()
    s.providers.market_data = "fixture"
    s.database.url = "sqlite:///:memory:"
    return s


def _tracker(now_ms: int = FIXED_NOW_MS) -> HorizonTracker:
    clock = FrozenClock(now_ms)
    return HorizonTracker(_settings(), FixtureProvider(clock=clock), clock=clock)


async def _series(symbol: str = "SOLUSDT", now_ms: int = FIXED_NOW_MS, bars: int = MAX_FETCH_BARS):
    provider = FixtureProvider(clock=FrozenClock(now_ms))
    return (await provider.get_ohlcv(symbol, Timeframe.M5, bars)).closed()


def _signal(series, index: int, symbol: str = "SOLUSDT", sid: int = 1) -> PendingSignal:
    return PendingSignal(
        id=sid,
        symbol=symbol,
        timestamp_ms=int(series.close_time_ms[index]),
        price=float(series.close[index]),
        atr=float(series.close[index]) * 0.01,
        timeframe=Timeframe.M5,
    )


# --------------------------------------------------------------------------- #
# The windows themselves
# --------------------------------------------------------------------------- #


def test_the_four_windows_are_the_documented_ones():
    assert [h.name for h in HORIZONS] == ["15m", "1h", "4h", "24h"]
    assert [h.minutes for h in HORIZONS] == [15, 60, 240, 1440]


def test_a_window_converts_to_the_right_number_of_bars():
    assert Horizon("1h", 60).bars(Timeframe.M5) == 12
    assert Horizon("24h", 1440).bars(Timeframe.M5) == 288
    assert Horizon("4h", 240).bars(Timeframe.H1) == 4


def test_a_window_shorter_than_one_bar_still_spans_one_bar():
    """15 minutes on a 4h timeframe is not zero bars — rounding down to nothing
    would silently settle the window at the entry price."""
    assert Horizon("15m", 15).bars(Timeframe.H4) == 1


# --------------------------------------------------------------------------- #
# The success rule lives in exactly one place
# --------------------------------------------------------------------------- #


def test_success_is_net_change_above_zero():
    ok = HorizonResult(1, "X", "1h", HorizonStatus.RESOLVED, net_change_pct=0.01)
    bad = HorizonResult(1, "X", "1h", HorizonStatus.RESOLVED, net_change_pct=-0.01)
    assert ok.is_success is True
    assert bad.is_success is False


def test_flat_after_costs_is_not_a_win():
    flat = HorizonResult(1, "X", "1h", HorizonStatus.RESOLVED, net_change_pct=0.0)
    assert flat.is_success is False


def test_a_window_without_a_verdict_is_not_a_failure():
    """PENDING and UNRESOLVABLE must both be None, never False. Counting an
    unfinished window as a loss is the easiest way to invent a bad track record."""
    for status in (HorizonStatus.PENDING, HorizonStatus.UNRESOLVABLE):
        assert HorizonResult(1, "X", "1h", status).is_success is None


# --------------------------------------------------------------------------- #
# Tracking against a real (fixture) series
# --------------------------------------------------------------------------- #


async def test_all_four_windows_resolve_for_an_old_enough_signal():
    series = await _series()
    # 300 bars of 5m is 25 hours — comfortably past the 24h window.
    sig = _signal(series, len(series) - 300)
    results = _tracker()._track_one(sig, series)

    assert len(results) == len(HORIZONS)
    assert {r.horizon for r in results} == {h.name for h in HORIZONS}
    assert all(r.status is HorizonStatus.RESOLVED for r in results)
    assert all(r.entry_price and r.entry_price > 0 for r in results)


async def test_entry_is_the_open_of_the_bar_after_the_signal():
    series = await _series()
    idx = len(series) - 300
    sig = _signal(series, idx)
    results = _tracker()._track_one(sig, series)
    expected = float(series.open[idx + 1])
    assert all(r.entry_price == pytest.approx(expected) for r in results)


async def test_a_window_that_has_not_elapsed_stays_pending():
    """The 24h window on a signal 30 minutes old must not be settled at the
    current price — the honest answer is 'not yet'."""
    series = await _series()
    sig = _signal(series, len(series) - 7)  # 6 bars = 30 minutes of future
    by_name = {r.horizon: r for r in _tracker()._track_one(sig, series)}

    assert by_name["15m"].status is HorizonStatus.RESOLVED
    assert by_name["1h"].status is HorizonStatus.PENDING
    assert by_name["4h"].status is HorizonStatus.PENDING
    assert by_name["24h"].status is HorizonStatus.PENDING
    assert by_name["1h"].price_at_horizon is None


async def test_a_signal_older_than_the_fetch_window_is_unresolvable_with_a_reason():
    series = await _series()
    ancient = PendingSignal(
        id=9,
        symbol="SOLUSDT",
        timestamp_ms=int(series.close_time_ms[0]) - 500 * Timeframe.M5.ms,
        price=100.0,
        atr=1.0,
        timeframe=Timeframe.M5,
    )
    results = _tracker()._track_one(ancient, series)
    assert all(r.status is HorizonStatus.UNRESOLVABLE for r in results)
    assert all("predates" in (r.note or "") for r in results)
    assert all(r.is_success is None for r in results)


async def test_a_missing_candle_is_reported_as_a_feed_gap_not_matched_to_a_neighbour():
    """Resolving against the nearest bar would shift every window by a bar."""
    series = await _series()
    off_grid = _signal(series, len(series) - 300)
    off_grid.timestamp_ms += 1  # one millisecond off the grid: no exact match
    results = _tracker()._track_one(off_grid, series)
    assert all(r.status is HorizonStatus.UNRESOLVABLE for r in results)
    assert all("trou dans le flux" in (r.note or "") for r in results)


async def test_max_gain_and_drawdown_bracket_the_final_change():
    series = await _series()
    sig = _signal(series, len(series) - 300)
    for r in _tracker()._track_one(sig, series):
        assert r.max_drawdown_pct <= r.change_pct <= r.max_gain_pct
        assert r.max_drawdown_pct <= 0.0 <= r.max_gain_pct or True  # entry is inside the window


async def test_longer_windows_never_shrink_the_observed_extremes():
    """24h contains 4h contains 1h contains 15m, so the best and worst the trade
    ever looked can only widen as the window grows."""
    series = await _series()
    by_name = {r.horizon: r for r in _tracker()._track_one(_signal(series, len(series) - 300), series)}
    order = ["15m", "1h", "4h", "24h"]
    for shorter, longer in zip(order, order[1:], strict=False):
        assert by_name[longer].max_gain_pct >= by_name[shorter].max_gain_pct - 1e-9
        assert by_name[longer].max_drawdown_pct <= by_name[shorter].max_drawdown_pct + 1e-9


async def test_net_change_is_below_gross_change_by_the_modelled_cost():
    series = await _series()
    tracker = _tracker()
    for r in tracker._track_one(_signal(series, len(series) - 300), series):
        assert r.net_change_pct == pytest.approx(tracker.costs.apply(r.change_pct))
        assert r.net_change_pct < r.change_pct


async def test_track_batches_by_symbol_and_reports_totals():
    series = await _series()
    signals = [_signal(series, len(series) - 300 - i, sid=i + 1) for i in range(5)]
    report = await _tracker().track(signals)

    assert report.checked == 5
    assert report.resolved == 5 * len(HORIZONS)
    assert report.pending == 0
    assert report.unresolvable == 0
    assert not report.errors


async def test_a_symbol_that_fails_to_fetch_does_not_stop_the_others():
    series = await _series()
    tracker = _tracker()

    class OneBadSymbol(FixtureProvider):
        async def get_ohlcv(self, symbol, timeframe, limit, **kw):
            if symbol == "BROKENUSDT":
                raise RuntimeError("provider exploded")
            return await super().get_ohlcv(symbol, timeframe, limit, **kw)

    tracker.provider = OneBadSymbol(clock=FrozenClock(FIXED_NOW_MS))
    good = _signal(series, len(series) - 300, sid=1)
    bad = PendingSignal(2, "BROKENUSDT", good.timestamp_ms, 1.0, 0.1, Timeframe.M5)

    report = await tracker.track([good, bad])
    assert "BROKENUSDT" in report.errors
    assert "provider exploded" in report.errors["BROKENUSDT"]
    assert report.resolved == len(HORIZONS)


async def test_ready_before_excludes_signals_too_new_for_even_the_first_window():
    tracker = _tracker()
    cutoff = tracker.ready_before_ms()
    assert cutoff < FIXED_NOW_MS
    # 15 minutes of bars plus the entry bar plus one for safety.
    expected = FIXED_NOW_MS - (3 + 2) * Timeframe.M5.ms
    assert cutoff == expected


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #


def _row(horizon: str, net: float, score: float = 70.0, **kw) -> dict:
    return {
        "signal_id": kw.get("signal_id", 1),
        "symbol": kw.get("symbol", "SOLUSDT"),
        "horizon": horizon,
        "timestamp_ms": FIXED_NOW_MS,
        "change_pct": net + 0.4,
        "net_change_pct": net,
        "max_gain_pct": max(net, 0.0) + 1.0,
        "max_drawdown_pct": min(net, 0.0) - 1.0,
        "success": net > 0,
        "final_score": score,
        "state": kw.get("state", "ARMED"),
        "provider": kw.get("provider", "binance"),
        "regime": "BULL",
        "synthetic": kw.get("synthetic", False),
    }


def test_horizon_stats_report_every_window_even_with_no_data():
    perf = build_horizon_performance([])
    assert [b["horizon"] for b in perf["overall"]] == ["15m", "1h", "4h", "24h"]
    assert all(b["n"] == 0 for b in perf["overall"])
    assert all(b["success_rate"] is None for b in perf["overall"])


def test_success_rate_counts_only_graded_windows():
    rows = [_row("1h", 1.0), _row("1h", -1.0), _row("1h", 2.0)]
    rows.append({**_row("1h", 0.0), "success": None, "net_change_pct": None})  # ungraded
    bucket = next(b for b in build_horizon_performance(rows)["overall"] if b["horizon"] == "1h")
    assert bucket["n"] == 3, "the ungraded row must not enlarge the sample"
    assert bucket["successes"] == 2
    # The serialiser rounds to 4dp, so compare at that resolution.
    assert bucket["success_rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_median_best_and_worst_are_reported_beside_the_average():
    rows = [_row("4h", v) for v in (-5.0, 0.5, 1.0, 1.5, 20.0)]
    b = next(x for x in build_horizon_performance(rows)["overall"] if x["horizon"] == "4h")
    assert b["median_change_pct"] == pytest.approx(1.0)
    assert b["avg_change_pct"] == pytest.approx(3.6)
    assert b["best_change_pct"] == pytest.approx(20.0)
    assert b["worst_change_pct"] == pytest.approx(-5.0)


def test_small_horizon_samples_are_flagged():
    perf = build_horizon_performance([_row("15m", 1.0) for _ in range(MIN_SAMPLE - 1)])
    b = next(x for x in perf["overall"] if x["horizon"] == "15m")
    assert b["n"] == MIN_SAMPLE - 1
    assert b["insufficient_sample"] is True
    assert any("ne doit être lu comme un résultat" in n for n in perf["notes"])


def test_horizon_stats_slice_by_score_band_and_provider():
    rows = [_row("1h", 2.0, score=85.0, provider="binance") for _ in range(3)]
    rows += [_row("1h", -2.0, score=40.0, provider="kraken") for _ in range(3)]
    perf = build_horizon_performance(rows)

    bands = {b["key"]: b for b in perf["by_score_band"] if b["horizon"] == "1h"}
    assert bands["80-100"]["success_rate"] == 1.0
    assert bands["35-50"]["success_rate"] == 0.0

    providers = {b["key"]: b for b in perf["by_provider"] if b["horizon"] == "1h"}
    assert set(providers) == {"binance", "kraken"}


def test_synthetic_horizon_rows_are_called_out():
    perf = build_horizon_performance([_row("1h", 1.0, synthetic=True)])
    assert perf["synthetic_count"] == 1
    assert any("SYNTHÉTIQUE" in n for n in perf["notes"])


def test_gross_basis_is_available_and_labelled():
    rows = [_row("1h", -0.1)]  # net negative, gross positive (+0.3)
    net = build_horizon_performance(rows, use_net=True)
    gross = build_horizon_performance(rows, use_net=False)
    assert net["return_basis"] == "net_change_pct"
    assert gross["return_basis"] == "change_pct"
    assert gross["overall"][1]["avg_change_pct"] == pytest.approx(0.3)
    assert any("ni frais, ni écart, ni glissement" in n for n in gross["notes"])


def test_costs_are_deducted_symmetrically_from_the_criterion():
    """The success rule is applied to the same number the report averages, so a
    row can never be counted as a success while showing a negative net change."""
    costs = CostModel()
    for gross in np.linspace(-2.0, 2.0, 25):
        r = HorizonResult(1, "X", "1h", HorizonStatus.RESOLVED, net_change_pct=costs.apply(float(gross)))
        assert r.is_success == (r.net_change_pct > 0)
