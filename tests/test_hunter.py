"""Token Hunter: does the wide pre-scan find what the classic scanner cannot?

The classic universe is the top 120 pairs by 24h volume, which excludes by
construction any token nobody is watching yet. The first test below is therefore
the one that matters: a small token whose activity has jumped must outrank a
large one trading normally. If that inverts, the module has no reason to exist.

The rest guard the honesty of the signals. A 24h rolling counter's delta is not
"volume in the last minute" — it is the excess over the same moment yesterday —
and the tests pin both the naming and the refusal to report a delta when there
is nothing to compare against.
"""

from __future__ import annotations

import pytest

from cryptopulse.core.clock import FrozenClock
from cryptopulse.core.types import Provenance, Ticker24h
from cryptopulse.hunter.discovery import (
    MIN_SECONDS_BETWEEN_SNAPSHOTS,
    SnapshotMemory,
    prescan,
)
from tests.conftest import FIXED_NOW_MS


def _ticker(
    symbol: str,
    *,
    price: float = 100.0,
    volume: float = 5_000_000.0,
    change: float = 1.0,
    high: float = 110.0,
    low: float = 90.0,
    trades: int = 20_000,
    bid: float | None = 99.95,
    ask: float | None = 100.05,
) -> Ticker24h:
    return Ticker24h(
        symbol=symbol, last_price=price, quote_volume_24h=volume,
        price_change_pct_24h=change, high_24h=high, low_24h=low, trades_24h=trades,
        provenance=Provenance(source="test", as_of_ms=FIXED_NOW_MS, fetched_at_ms=FIXED_NOW_MS),
        bid=bid, ask=ask,
    )


def _memory(now_ms: int = FIXED_NOW_MS) -> SnapshotMemory:
    return SnapshotMemory(clock=FrozenClock(now_ms))


def _two_readings(first: dict, second: dict, gap_s: float = 60.0):
    """Run two cycles a real interval apart, returning the second report."""
    mem = _memory()
    prescan(first, mem, now_ms=FIXED_NOW_MS)
    return prescan(second, mem, now_ms=FIXED_NOW_MS + int(gap_s * 1000))


# --------------------------------------------------------------------------- #
# The inversion that justifies the module
# --------------------------------------------------------------------------- #


def test_a_small_accelerating_token_outranks_a_large_quiet_one():
    """The whole point. The volume-sorted universe would rank these the other
    way round and never look at the small one at all."""
    before = {
        "BIGUSDT": _ticker("BIGUSDT", volume=20_000_000_000.0, trades=9_000_000),
        "SMALLUSDT": _ticker("SMALLUSDT", volume=3_000_000.0, trades=8_000),
    }
    after = {
        # The whale trades on, entirely unremarkably.
        "BIGUSDT": _ticker("BIGUSDT", volume=20_000_400_000.0, trades=9_000_180),
        # The minnow's activity has jumped against the same moment yesterday.
        "SMALLUSDT": _ticker("SMALLUSDT", volume=3_240_000.0, trades=8_900),
    }

    report = _two_readings(before, after)
    ranked = [c.symbol for c in report.candidates]
    assert ranked[0] == "SMALLUSDT", f"anomaly must outrank size, got {ranked}"

    small = report.candidates[0]
    assert small.volume_excess_vs_yesterday > 5.0
    assert any("above the same moment yesterday" in r for r in small.reasons)


def test_ranking_is_not_a_volume_ordering():
    tickers = {
        f"T{i}USDT": _ticker(f"T{i}USDT", volume=float(10 ** (6 + i % 4)))
        for i in range(8)
    }
    report = prescan(tickers, _memory(), now_ms=FIXED_NOW_MS)
    volumes = [c.quote_volume_24h for c in report.candidates]
    assert volumes != sorted(volumes, reverse=True), "the hunter reproduced a volume sort"


# --------------------------------------------------------------------------- #
# Honesty about the acceleration signal
# --------------------------------------------------------------------------- #


def test_the_first_reading_reports_no_acceleration_rather_than_zero():
    """There is nothing to compare against yet. A delta of 0.0 would be a
    fabrication — it would read as 'activity is exactly normal'."""
    report = prescan({"AAAUSDT": _ticker("AAAUSDT")}, _memory(), now_ms=FIXED_NOW_MS)

    assert report.has_previous_reading is False
    c = report.candidates[0]
    assert c.volume_excess_vs_yesterday is None
    assert c.trade_excess_vs_yesterday is None
    assert c.seconds_since_previous is None
    assert any("no previous reading" in x for x in c.caveats)
    assert any("no previous snapshot" in n for n in report.notes)


def test_readings_too_close_together_produce_no_delta():
    """A few seconds of trades against a 24-hour window is noise, not a signal."""
    t = {"AAAUSDT": _ticker("AAAUSDT")}
    report = _two_readings(t, t, gap_s=MIN_SECONDS_BETWEEN_SNAPSHOTS - 1)
    assert report.candidates[0].volume_excess_vs_yesterday is None
    assert report.candidates[0].seconds_since_previous is None


def test_a_delta_appears_once_the_readings_are_far_enough_apart():
    before = {"AAAUSDT": _ticker("AAAUSDT", volume=1_000_000.0)}
    after = {"AAAUSDT": _ticker("AAAUSDT", volume=1_100_000.0)}
    c = _two_readings(before, after, gap_s=60.0).candidates[0]

    assert c.seconds_since_previous == pytest.approx(60.0)
    assert c.volume_excess_vs_yesterday == pytest.approx(10.0)


def test_a_token_trading_normally_shows_almost_no_excess():
    """A steady 24h counter barely moves between readings — which is exactly what
    makes the delta an anomaly detector rather than a size measure."""
    before = {"AAAUSDT": _ticker("AAAUSDT", volume=10_000_000.0)}
    after = {"AAAUSDT": _ticker("AAAUSDT", volume=10_001_000.0)}
    c = _two_readings(before, after).candidates[0]
    assert abs(c.volume_excess_vs_yesterday) < 0.05


def test_volume_rising_while_price_is_flat_is_called_out():
    """The shape the product exists to find: activity before the move."""
    before = {"AAAUSDT": _ticker("AAAUSDT", price=100.0, volume=1_000_000.0)}
    after = {"AAAUSDT": _ticker("AAAUSDT", price=100.05, volume=1_150_000.0)}
    c = _two_readings(before, after).candidates[0]
    assert any("still flat" in r for r in c.reasons)


# --------------------------------------------------------------------------- #
# It looks for early moves, not finished ones
# --------------------------------------------------------------------------- #


def test_a_token_that_already_ran_is_penalised_not_promoted():
    calm = _ticker("CALMUSDT", change=1.0, price=100.0, high=110.0, low=90.0)
    spent = _ticker("SPENTUSDT", change=42.0, price=142.0, high=143.0, low=100.0)
    report = prescan({"CALMUSDT": calm, "SPENTUSDT": spent}, _memory(), now_ms=FIXED_NOW_MS)

    by = {c.symbol: c for c in report.candidates}
    assert by["CALMUSDT"].priority > by["SPENTUSDT"].priority
    assert any("may be spent" in x for x in by["SPENTUSDT"].caveats)


def test_being_pinned_at_the_24h_high_is_a_caveat_not_a_reason():
    at_high = _ticker("AAAUSDT", price=109.8, high=110.0, low=90.0)
    c = prescan({"AAAUSDT": at_high}, _memory(), now_ms=FIXED_NOW_MS).candidates[0]
    assert any("24h range" in x for x in c.caveats)
    assert not any("not pinned at the high" in r for r in c.reasons)


# --------------------------------------------------------------------------- #
# Tradeability floors
# --------------------------------------------------------------------------- #


def test_untradeable_volume_is_rejected_and_counted():
    tickers = {
        "OKUSDT": _ticker("OKUSDT", volume=5_000_000.0),
        "DUSTUSDT": _ticker("DUSTUSDT", volume=1_000.0),
    }
    report = prescan(tickers, _memory(), now_ms=FIXED_NOW_MS)
    assert [c.symbol for c in report.candidates] == ["OKUSDT"]
    assert report.rejected["untradeable_volume"] == 1
    assert report.eligible == 1


def test_a_ruinous_spread_is_rejected():
    tickers = {
        "OKUSDT": _ticker("OKUSDT"),
        "WIDEUSDT": _ticker("WIDEUSDT", price=100.0, bid=90.0, ask=110.0),
    }
    report = prescan(tickers, _memory(), now_ms=FIXED_NOW_MS)
    assert [c.symbol for c in report.candidates] == ["OKUSDT"]
    assert report.rejected["spread_too_wide"] == 1


def test_stablecoins_and_leveraged_tokens_are_excluded():
    tickers = {
        "OKUSDT": _ticker("OKUSDT"),
        "USDCUSDT": _ticker("USDCUSDT"),
        "BTCUPUSDT": _ticker("BTCUPUSDT"),
        "ETHBTC": _ticker("ETHBTC"),
    }
    report = prescan(
        tickers, _memory(), quote_asset="USDT",
        exclude_bases=("USDC",), exclude_patterns=("UP", "DOWN"), now_ms=FIXED_NOW_MS,
    )
    assert [c.symbol for c in report.candidates] == ["OKUSDT"]
    assert report.rejected["stablecoin"] == 1
    assert report.rejected["leveraged_token"] == 1
    assert report.rejected["wrong_quote"] == 1


def test_a_missing_spread_is_unknown_rather_than_perfect():
    """A venue that publishes no bid/ask must not read as a zero spread, which
    would look like the tightest market on the board."""
    c = prescan(
        {"AAAUSDT": _ticker("AAAUSDT", bid=None, ask=None)}, _memory(), now_ms=FIXED_NOW_MS
    ).candidates[0]
    assert c.spread_bps is None
    assert any("spread unknown" in x for x in c.caveats)
    assert not any("tight spread" in r for r in c.reasons)


# --------------------------------------------------------------------------- #
# Universal properties
# --------------------------------------------------------------------------- #


def test_every_candidate_explains_itself():
    """A ranked row with no reason and no caveat is an unexplained opinion."""
    tickers = {f"T{i}USDT": _ticker(f"T{i}USDT", change=float(i), price=95.0 + i) for i in range(10)}
    for c in prescan(tickers, _memory(), now_ms=FIXED_NOW_MS).candidates:
        assert c.reasons or c.caveats, f"{c.symbol} was ranked without saying why"


def test_the_prescan_costs_no_requests():
    """It reads the ticker dictionary the scan already fetched. That is what
    makes searching hundreds of tokens viable on a phone."""
    report = prescan({"AAAUSDT": _ticker("AAAUSDT")}, _memory(), now_ms=FIXED_NOW_MS)
    assert report.requests_used == 0


def test_the_limit_is_respected_and_the_best_are_kept():
    tickers = {f"T{i}USDT": _ticker(f"T{i}USDT", price=91.0 + i * 0.5) for i in range(60)}
    report = prescan(tickers, _memory(), limit=10, now_ms=FIXED_NOW_MS)
    assert len(report.candidates) == 10
    assert report.eligible == 60
    priorities = [c.priority for c in report.candidates]
    assert priorities == sorted(priorities, reverse=True)


def test_an_empty_venue_produces_an_empty_report_not_an_error():
    report = prescan({}, _memory(), now_ms=FIXED_NOW_MS)
    assert report.universe_size == 0
    assert report.candidates == []
    assert report.has_previous_reading is False


def test_the_memory_holds_one_row_per_symbol_not_a_history():
    mem = _memory()
    tickers = {f"T{i}USDT": _ticker(f"T{i}USDT") for i in range(5)}
    for cycle in range(4):
        prescan(tickers, mem, now_ms=FIXED_NOW_MS + cycle * 60_000)
    assert mem.size == 5, "the snapshot memory must not grow with time"


def test_serialisation_names_the_signal_for_what_it_measures():
    """`volume_excess_vs_yesterday`, never `volume_1m`. A rolling-window delta
    is not recent volume, and a field name is a claim."""
    d = prescan({"AAAUSDT": _ticker("AAAUSDT")}, _memory(), now_ms=FIXED_NOW_MS).candidates[0].to_dict()
    assert "volume_excess_vs_yesterday" in d
    assert not any(k.startswith("volume_1") or k.startswith("volume_5") for k in d)


# --------------------------------------------------------------------------- #
# A read must never disturb the signal
# --------------------------------------------------------------------------- #


def test_reading_the_search_does_not_erase_the_acceleration_signal():
    """Found by running the service, not by reasoning about it.

    The first version recorded a snapshot on every call, so an on-demand search
    a few seconds after a scan overwrote the previous reading — and the very
    acceleration the search exists to surface disappeared the moment it was
    asked for. Reads are now non-recording.
    """
    mem = _memory()
    before = {"AAAUSDT": _ticker("AAAUSDT", volume=1_000_000.0)}
    after = {"AAAUSDT": _ticker("AAAUSDT", volume=1_100_000.0)}

    prescan(before, mem, now_ms=FIXED_NOW_MS, record=True)
    cycle = prescan(after, mem, now_ms=FIXED_NOW_MS + 60_000, record=True)
    assert cycle.candidates[0].volume_excess_vs_yesterday == pytest.approx(10.0)

    # Five reads seconds apart: the stored reading must not move.
    for i in range(5):
        read = prescan(after, mem, now_ms=FIXED_NOW_MS + 62_000 + i * 1_000, record=False)
        assert read.candidates[0].volume_excess_vs_yesterday is None, (
            "a read this close to the cycle has no delta of its own — expected"
        )

    # And the cycle after that still measures against the recorded reading.
    later = {"AAAUSDT": _ticker("AAAUSDT", volume=1_210_000.0)}
    nxt = prescan(later, mem, now_ms=FIXED_NOW_MS + 120_000, record=True)
    assert nxt.candidates[0].volume_excess_vs_yesterday == pytest.approx(10.0), (
        "the reads corrupted the snapshot memory"
    )


def test_a_read_still_reports_the_report_timestamp():
    report = prescan({"AAAUSDT": _ticker("AAAUSDT")}, _memory(), now_ms=FIXED_NOW_MS, record=False)
    assert report.computed_at_ms == FIXED_NOW_MS
    assert report.to_dict()["computed_at_ms"] == FIXED_NOW_MS
