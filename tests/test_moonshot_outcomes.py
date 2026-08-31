"""Grading the ×10 axis: the journal, the label, and the two independent verdicts.

Before this existed the moonshot reading was computed, displayed, alerted — and
thrown away. A layer with no history cannot be validated, ever, so the tests
here are mostly about the reading surviving to disk and coming back with a
verdict that matches its own horizon rather than someone else's.
"""

from __future__ import annotations

import numpy as np
import pytest

from cryptopulse.backtest.labels import (
    MOONSHOT_LABEL_CONFIGS,
    LabelConfig,
    Outcome,
    label_config_by_name,
    label_signal,
)
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.core.types import Timeframe
from cryptopulse.database import repo
from cryptopulse.database.session import init_engine, reset_engine
from cryptopulse.outcomes.stats import build_moonshot_performance
from cryptopulse.outcomes.tracker import OutcomeTracker, PendingSignal
from cryptopulse.providers.fixture import FixtureProvider
from cryptopulse.scanner.cex import CexScanner
from tests.conftest import FIXED_NOW_MS

MOON = label_config_by_name("moon_2x_30d")


@pytest.fixture()
def journal(tmp_path):
    reset_engine()
    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.scanner.universe = "robinhood"
    settings.database.url = f"sqlite:///{tmp_path}/journal.db"
    init_engine(settings.database)
    yield settings
    reset_engine()


# --------------------------------------------------------------------------- #
# The label: a ×N thesis stated in multiples, not in ATR
# --------------------------------------------------------------------------- #


def _bars(closes):
    c = np.asarray(closes, dtype=np.float64)
    o = np.concatenate([[c[0]], c[:-1]])
    return np.maximum(o, c), np.minimum(o, c), c, o  # high, low, close, open


def test_a_multiple_label_places_its_barriers_on_price_not_on_atr():
    high, low, close, open_ = _bars([10.0, 10.0, 12.0, 15.0, 21.0, 22.0])
    cfg = LabelConfig("x2", target_multiple=2.0, stop_pct=50.0, horizon_bars=5)
    r = label_signal(high, low, close, open_, 0, atr_at_signal=0.0, cfg=cfg)

    assert r.outcome is Outcome.WIN
    assert r.exit_price == pytest.approx(20.0)  # entry 10 -> target x2
    assert r.max_multiple >= 2.0


def test_a_multiple_label_does_not_need_an_atr():
    """ATR barriers are unplaceable without an ATR; price barriers are not."""
    high, low, close, open_ = _bars([10.0] + [10.5] * 40)
    cfg = MOON
    assert cfg.needs_atr is False
    r = label_signal(high, low, close, open_, 0, atr_at_signal=0.0, cfg=cfg)
    assert r.outcome is Outcome.TIMEOUT
    # Excursions are unknown without a scale, and say so rather than reporting 0.
    assert not np.isfinite(r.mfe_atr)


def test_the_stop_is_a_percentage_of_entry():
    high, low, close, open_ = _bars([10.0, 10.0, 8.0, 6.0, 6.0])
    cfg = LabelConfig("x2", target_multiple=2.0, stop_pct=35.0, horizon_bars=4)
    r = label_signal(high, low, close, open_, 0, 0.0, cfg)
    assert r.outcome is Outcome.LOSS
    assert r.exit_price == pytest.approx(6.5)


def test_max_multiple_is_recorded_whatever_the_verdict():
    """The field that keeps the ×10 question answerable without a ×10 label."""
    high, low, close, open_ = _bars([10.0, 10.0, 28.0, 12.0, 11.0, 11.0])
    cfg = LabelConfig("x50", target_multiple=50.0, stop_pct=90.0, horizon_bars=5)
    r = label_signal(high, low, close, open_, 0, 0.0, cfg)

    assert r.outcome is Outcome.TIMEOUT  # never reached x50
    assert r.max_multiple == pytest.approx(2.8)  # but it did reach x2.8


def test_the_ladder_is_daily_and_ordered():
    assert [c.name for c in MOONSHOT_LABEL_CONFIGS] == ["moon_2x_30d", "moon_3x_90d", "moon_10x_180d"]
    for cfg in MOONSHOT_LABEL_CONFIGS:
        assert cfg.timeframe is Timeframe.D1
        assert cfg.is_multiple_based
        assert "max_multiple" in cfg.describe()


def test_an_unknown_label_name_raises_rather_than_silently_falling_back():
    with pytest.raises(ValueError, match="unknown label config"):
        label_config_by_name("does_not_exist")
    assert label_config_by_name("does_not_exist", MOON) is MOON


# --------------------------------------------------------------------------- #
# The tracker: grading a 5-minute signal on daily bars
# --------------------------------------------------------------------------- #


def _tracker(settings, label, now_ms=FIXED_NOW_MS) -> OutcomeTracker:
    clock = FrozenClock(now_ms)
    return OutcomeTracker(settings, FixtureProvider(clock=clock), label_config=label, clock=clock)


def test_the_tracker_grades_on_the_labels_timeframe_not_the_scanners():
    settings = CryptoPulseSettings()
    assert settings.scanner.primary_timeframe is Timeframe.M5
    assert _tracker(settings, MOON).timeframe is Timeframe.D1
    assert _tracker(settings, label_config_by_name("standard_2R")).timeframe is Timeframe.M5


def test_a_weeks_long_horizon_reaches_back_further_than_the_intraday_one():
    """3.5 days of reach cannot grade a 30-day thesis; that was the whole bug."""
    settings = CryptoPulseSettings()
    intraday = _tracker(settings, label_config_by_name("standard_2R"))
    moon = _tracker(settings, MOON)
    intraday_reach = FIXED_NOW_MS - intraday.unresolvable_before_ms()
    moon_reach = FIXED_NOW_MS - moon.unresolvable_before_ms()
    assert moon_reach > intraday_reach * 100


async def test_a_five_minute_signal_is_placed_in_the_daily_bar_it_fired_inside():
    settings = CryptoPulseSettings()
    tracker = _tracker(settings, MOON)
    series = (await tracker.provider.get_ohlcv("BTCUSDT", Timeframe.D1, 400)).closed()

    # A signal timestamped mid-way through a daily bar, as a 5m close would be.
    bar = 300
    inside = int(series.close_time_ms[bar]) - 6 * 3_600_000
    sig = PendingSignal(id=1, symbol="BTCUSDT", timestamp_ms=inside, price=1.0, atr=None, timeframe=Timeframe.M5)

    idx, note, failure = tracker._entry_index(sig, series)
    assert failure is None
    assert idx == bar, "the signal belongs to the daily bar that was still forming"
    assert note and "entry is the open of the next one" in note


async def test_a_same_timeframe_signal_still_demands_an_exact_bar():
    """The V1 guarantee is untouched: no approximating a neighbour."""
    settings = CryptoPulseSettings()
    tracker = _tracker(settings, label_config_by_name("standard_2R"))
    series = (await tracker.provider.get_ohlcv("BTCUSDT", Timeframe.M5, 500)).closed()

    missing = int(series.close_time_ms[200]) + 1  # a timestamp no bar closed at
    sig = PendingSignal(id=1, symbol="BTCUSDT", timestamp_ms=missing, price=1.0, atr=1.0, timeframe=Timeframe.M5)
    idx, _note, failure = tracker._entry_index(sig, series)
    assert idx is None
    assert "feed gap" in failure


# --------------------------------------------------------------------------- #
# The journal
# --------------------------------------------------------------------------- #


async def test_a_scan_writes_the_moonshot_reading_to_the_journal(journal):
    clock = FrozenClock(FIXED_NOW_MS)
    scanner = CexScanner(journal, provider=FixtureProvider(clock=clock), clock=clock)
    report = await scanner.scan()
    repo.persist_scan(report, provider="fixture", regime="RANGE")
    await scanner.close()

    counts = repo.moonshot_counts()
    assert counts["readings_journalled"] > 0
    assert counts["pending_evaluation"] == counts["readings_journalled"]
    assert counts["settled"] == 0
    assert counts["reached_10x"] == 0

    rows = repo.recent_signals(limit=50)
    scored = [r for r in rows if r.get("moonshot_score") is not None] if rows else []
    # recent_signals may not expose the column; the counts above already prove it.
    assert counts["readings_journalled"] >= len(scored)


def test_a_dormant_base_is_journalled_even_with_no_intraday_setup(journal):
    """The assets the ×10 layer exists to find score IGNORE on the setup axis.

    Filtering the journal on the setup state alone guaranteed an empty moonshot
    history and therefore a permanently unvalidatable layer.
    """
    from types import SimpleNamespace

    from cryptopulse.scoring.moonshot import MoonshotAssessment, MoonshotStage
    from cryptopulse.scoring.states import SetupState

    def result(stage, moon_score, setup_state):
        return SimpleNamespace(
            state=SimpleNamespace(state=setup_state),
            moonshot=MoonshotAssessment(
                score=moon_score, ignition=moon_score, headroom=50.0, capacity=None,
                stage=stage, timeframe="1d",
            ),
        )

    ignored_but_based = result(MoonshotStage.ACCUMULATION, 72.0, SetupState.IGNORE)
    ignored_and_dull = result(MoonshotStage.NEUTRAL, 72.0, SetupState.IGNORE)
    weak_reading = result(MoonshotStage.ACCUMULATION, 12.0, SetupState.IGNORE)
    ordinary_setup = result(MoonshotStage.NEUTRAL, 5.0, SetupState.ARMED)

    assert repo._should_persist(ignored_but_based, 50.0) is True
    assert repo._should_persist(ordinary_setup, 50.0) is True
    assert repo._should_persist(ignored_and_dull, 50.0) is False
    assert repo._should_persist(weak_reading, 50.0) is False


async def test_the_two_axes_are_graded_independently(journal):
    """A signal settled on the intraday axis is still pending on the ×10 one."""
    clock = FrozenClock(FIXED_NOW_MS)
    scanner = CexScanner(journal, provider=FixtureProvider(clock=clock), clock=clock)
    report = await scanner.scan()
    repo.persist_scan(report, provider="fixture", regime="RANGE")
    await scanner.close()

    # Grade the intraday axis a few hours later.
    setup_label = label_config_by_name("standard_2R")
    later = FIXED_NOW_MS + 40 * Timeframe.M5.ms
    setup_tracker = _tracker(journal, setup_label, later)
    setup_pending = repo.pending_signals(setup_tracker.ready_before_ms(), 100)
    assert setup_pending
    repo.save_resolutions((await setup_tracker.resolve(setup_pending)).resolutions)

    assert repo.outcome_counts()["settled"] > 0
    # The ×10 verdict is untouched: its horizon has not elapsed.
    assert repo.moonshot_counts()["settled"] == 0
    assert repo.moonshot_counts()["pending_evaluation"] > 0


async def test_a_moonshot_verdict_is_written_once_and_never_re_graded(journal):
    clock = FrozenClock(FIXED_NOW_MS)
    scanner = CexScanner(journal, provider=FixtureProvider(clock=clock), clock=clock)
    report = await scanner.scan()
    repo.persist_scan(report, provider="fixture", regime="RANGE")
    await scanner.close()

    later = FIXED_NOW_MS + 40 * 86_400_000
    tracker = _tracker(journal, MOON, later)
    pending = repo.pending_moonshot_signals(tracker.ready_before_ms(), 500)
    assert pending, "30 daily bars have elapsed, so these are gradable"

    resolutions = (await tracker.resolve(pending)).resolutions
    assert repo.save_moonshot_resolutions(resolutions) == len(resolutions)
    # Re-running writes nothing: history is not rewritten under a second pass.
    assert repo.save_moonshot_resolutions(resolutions) == 0

    settled = repo.resolved_moonshot_signals()
    assert settled
    row = settled[0]
    assert row["max_multiple"] is not None
    assert row["outcome"] in ("WIN", "LOSS", "TIMEOUT")
    assert row["label_config"] == "moon_2x_30d"
    assert row["moonshot_stage"]


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def test_the_report_leads_with_the_distribution_not_the_win_rate():
    rows = [
        {"outcome": "WIN", "net_return_pct": 100.0, "max_multiple": 2.4, "moonshot_score": 71,
         "moonshot_stage": "IGNITION", "moonshot_capacity": 80.0, "label_config": "moon_2x_30d"},
        {"outcome": "LOSS", "net_return_pct": -35.0, "max_multiple": 1.1, "moonshot_score": 62,
         "moonshot_stage": "ACCUMULATION", "moonshot_capacity": None, "label_config": "moon_2x_30d"},
        {"outcome": "TIMEOUT", "net_return_pct": 12.0, "max_multiple": 1.8, "moonshot_score": 55,
         "moonshot_stage": "ACCUMULATION", "moonshot_capacity": None, "label_config": "moon_2x_30d"},
    ]
    perf = build_moonshot_performance(rows).to_dict()

    dist = {d["at_least"]: d["n"] for d in perf["multiple_distribution"]}
    assert dist[1.5] == 2 and dist[2.0] == 1 and dist[10.0] == 0
    assert perf["best_multiple"] == 2.4
    # Reported even — especially — when nothing reached the target.
    assert any("No reading has reached x10" in n for n in perf["notes"])
    # And whether the market cap reading separated anything is asked out loud.
    assert {b["key"] for b in perf["by_capacity_known"]} == {"capacity known", "capacity unknown"}


def test_an_empty_report_is_empty_rather_than_zero_filled():
    perf = build_moonshot_performance([]).to_dict()
    assert perf["overall"]["n"] == 0
    assert perf["overall"]["win_rate"] is None
    assert perf["best_multiple"] is None
