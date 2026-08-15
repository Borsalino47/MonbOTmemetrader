"""Backtest labelling, metrics, splits and the replay's freedom from look-ahead."""

from __future__ import annotations

import numpy as np
import pytest

from cryptopulse.backtest.engine import BacktestConfig, BacktestEngine, CostModel
from cryptopulse.backtest.labels import LabelConfig, Outcome, label_signal
from cryptopulse.backtest.metrics import TradeRecord, compute_metrics
from cryptopulse.backtest.splits import chronological_split, walk_forward_windows
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.core.types import Timeframe
from cryptopulse.providers.fixture import FixtureProvider
from tests.conftest import FIXED_NOW_MS

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #

CFG = LabelConfig("test", target_atr=2.0, stop_atr=1.0, horizon_bars=10)


def _arrays(n=30, base=100.0):
    o = np.full(n, base)
    h = np.full(n, base)
    l = np.full(n, base)
    c = np.full(n, base)
    return o, h, l, c


async def test_label_win_when_target_is_hit_first():
    o, h, l, c = _arrays()
    h[5] = 103.0  # entry at index 1 open (=100), target = 100 + 2*1 = 102
    result = label_signal(h, l, c, o, signal_index=0, atr_at_signal=1.0, cfg=CFG)
    assert result.outcome is Outcome.WIN
    assert result.entry_price == 100.0
    assert result.exit_price == 102.0


async def test_label_loss_when_stop_is_hit_first():
    o, h, l, c = _arrays()
    l[3] = 98.0  # stop = 100 - 1*1 = 99
    result = label_signal(h, l, c, o, signal_index=0, atr_at_signal=1.0, cfg=CFG)
    assert result.outcome is Outcome.LOSS
    assert result.exit_price == 99.0


async def test_ambiguous_bar_resolves_pessimistically():
    """Both barriers touched in one candle: intrabar order is unknown, assume loss."""
    o, h, l, c = _arrays()
    h[4] = 105.0
    l[4] = 97.0
    result = label_signal(h, l, c, o, signal_index=0, atr_at_signal=1.0, cfg=CFG)
    assert result.outcome is Outcome.LOSS, "an unknowable sequence must not be scored as a win"


async def test_label_timeout_when_neither_barrier_is_touched():
    o, h, l, c = _arrays(n=30)
    result = label_signal(h, l, c, o, signal_index=0, atr_at_signal=1.0, cfg=CFG)
    assert result.outcome is Outcome.TIMEOUT
    assert result.return_pct == pytest.approx(0.0)


async def test_label_unresolved_when_the_future_does_not_exist_yet():
    """A signal near the end of history is unknown, not a timeout."""
    o, h, l, c = _arrays(n=5)
    result = label_signal(h, l, c, o, signal_index=3, atr_at_signal=1.0, cfg=CFG)
    assert result.outcome is Outcome.UNRESOLVED


async def test_label_entry_is_the_next_bar_open_not_the_signal_close():
    o, h, l, c = _arrays()
    o[1] = 101.5  # the fill we would actually get
    c[0] = 100.0
    result = label_signal(h, l, c, o, signal_index=0, atr_at_signal=1.0, cfg=CFG)
    assert result.entry_price == 101.5


async def test_label_records_excursions():
    o, h, l, c = _arrays()
    h[2] = 101.5
    l[3] = 99.5
    h[6] = 103.0
    result = label_signal(h, l, c, o, signal_index=0, atr_at_signal=1.0, cfg=CFG)
    assert result.mfe_atr > 0 and result.mae_atr < 0


async def test_label_config_documents_itself():
    text = CFG.describe()
    assert "next bar's open" in text and "2.0 ATR" in text and "LOSS" in text


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def _trade(ret, outcome="WIN", regime=None, i=0):
    return TradeRecord(
        symbol="X", entry_time_ms=i, entry_price=100, exit_price=100 * (1 + ret / 100),
        return_pct=ret, gross_return_pct=ret, outcome=outcome, bars_held=5,
        mfe_atr=1.0, mae_atr=-0.5, score=70.0, regime=regime,
    )


async def test_metrics_on_an_empty_set_say_so():
    m = compute_metrics([])
    assert m.trades == 0 and m.win_rate is None
    assert "no trades" in m.notes[0]


async def test_expectancy_and_profit_factor():
    trades = [_trade(10.0, i=0), _trade(10.0, i=1), _trade(-5.0, "LOSS", i=2), _trade(-5.0, "LOSS", i=3)]
    m = compute_metrics(trades)
    assert m.win_rate == pytest.approx(0.5)
    assert m.expectancy_pct == pytest.approx(2.5)
    assert m.profit_factor == pytest.approx(2.0)


async def test_ratios_omitted_on_a_small_sample():
    m = compute_metrics([_trade(5.0, i=i) for i in range(5)])
    assert m.sharpe is None and m.sortino is None
    assert any("below the" in n for n in m.notes)


async def test_ratios_computed_once_the_sample_is_large_enough():
    rng = np.random.default_rng(61)
    trades = [_trade(float(r), "WIN" if r > 0 else "LOSS", i=i) for i, r in enumerate(rng.normal(1.0, 3.0, 40))]
    m = compute_metrics(trades)
    assert m.sharpe is not None
    assert any("not annualised" in n for n in m.notes)


async def test_max_drawdown_is_negative_after_a_losing_run():
    trades = [_trade(10.0, i=0), _trade(-20.0, "LOSS", i=1), _trade(-20.0, "LOSS", i=2)]
    m = compute_metrics(trades)
    assert m.max_drawdown_pct < 0


async def test_metrics_break_down_by_regime():
    trades = [_trade(5.0, regime="BULL_TREND", i=0), _trade(-5.0, "LOSS", regime="BEAR_TREND", i=1)]
    m = compute_metrics(trades)
    assert set(m.by_regime) == {"BULL_TREND", "BEAR_TREND"}
    assert m.by_regime["BULL_TREND"]["trades"] == 1


async def test_undefined_profit_factor_is_reported_not_faked():
    m = compute_metrics([_trade(5.0, i=0), _trade(3.0, i=1)])
    assert m.profit_factor is None
    assert any("undefined" in n for n in m.notes)


# --------------------------------------------------------------------------- #
# Splits
# --------------------------------------------------------------------------- #


async def test_chronological_split_is_ordered_and_contiguous():
    splits = chronological_split(1000)
    assert [s.name for s in splits] == ["train", "validation", "out_of_sample"]
    assert splits[0].start == 0 and splits[-1].end == 1000
    for a, b in zip(splits, splits[1:], strict=False):
        assert a.end == b.start, "splits must not overlap or leave holes"
    assert splits[0].size == 600 and splits[1].size == 200


async def test_walk_forward_windows_never_overlap_train_and_test():
    windows = walk_forward_windows(1000, train_size=400, test_size=100, embargo=24)
    assert windows
    for train, test in windows:
        assert test.start >= train.end + 24, "embargo must separate train from test"
        assert train.end <= test.start
    starts = [t.start for _, t in windows]
    assert starts == sorted(starts), "windows must march forward in time"


async def test_walk_forward_stops_before_running_off_the_end():
    for _, test in walk_forward_windows(500, train_size=200, test_size=50):
        assert test.end <= 500


# --------------------------------------------------------------------------- #
# Cost model
# --------------------------------------------------------------------------- #


async def test_costs_reduce_every_return():
    costs = CostModel()
    assert costs.apply(10.0) < 10.0
    assert costs.apply(-5.0) < -5.0
    assert costs.round_trip_bps == pytest.approx(40.0)


async def test_cost_assumptions_are_disclosed():
    d = CostModel().describe()
    assert "taker_fee_bps" in d and "note" in d
    assert "slippage" in d["note"].lower()


# --------------------------------------------------------------------------- #
# End-to-end replay
# --------------------------------------------------------------------------- #


async def test_backtest_runs_and_labels_its_synthetic_source():
    settings = CryptoPulseSettings()
    provider = FixtureProvider(clock=FrozenClock(FIXED_NOW_MS), include_open_candle=False)
    by_tf = {}
    for tf in (Timeframe.M5, Timeframe.H1, Timeframe.H4):
        by_tf[tf] = await provider.get_ohlcv("SOLUSDT", tf, 700)

    engine = BacktestEngine(settings, BacktestConfig(min_score=55.0, warmup_bars=260, cooldown_bars=20))
    result = engine.run(
        {"SOLUSDT": by_tf}, primary_tf=Timeframe.M5, data_source=provider.name, synthetic=True
    )

    assert result.synthetic is True
    assert "SYNTHETIC" in (result.to_dict()["warning"] or "")
    assert result.config["engine_version"] == "SCORE_ENGINE_V1"
    assert "costs" in result.config
    # The point is that the machinery runs end to end and reports honestly,
    # not that the synthetic numbers mean anything.
    assert result.signals_generated >= 0
    assert result.metrics.trades == len(result.trades)


async def test_backtest_never_sees_a_bar_it_should_not_have():
    """Replay slices are strictly historical: no candle after the decision time."""
    settings = CryptoPulseSettings()
    provider = FixtureProvider(clock=FrozenClock(FIXED_NOW_MS), include_open_candle=False)
    series = await provider.get_ohlcv("BTCUSDT", Timeframe.M5, 400)

    engine = BacktestEngine(settings)
    decision_ts = int(series.close_time_ms[300])
    af = engine._build_features_at({Timeframe.M5: series}, Timeframe.M5, decision_ts, 5e7)

    assert af is not None
    primary = af.primary
    assert primary.close_time_ms <= decision_ts
    assert primary.bars == 301, "should see exactly the bars closed by the decision time"
    assert primary.close == pytest.approx(float(series.close[300]))
