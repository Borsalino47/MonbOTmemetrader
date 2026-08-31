"""The candle cache: it must save requests without ever changing an answer.

A cache in front of market data is only acceptable if it is provably
indistinguishable from the source for the data anything actually reads. The
first test states that property; the rest defend the boundary conditions where
it could quietly stop holding.
"""

from __future__ import annotations

import pytest

from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.core.types import Timeframe
from cryptopulse.providers.base import OrderBookProvider
from cryptopulse.providers.cache import CachingMarketDataProvider, wrap_with_cache
from cryptopulse.providers.fixture import FixtureProvider
from cryptopulse.providers.registry import build_market_provider, is_synthetic
from tests.conftest import FIXED_NOW_MS


class _Counting(FixtureProvider):
    """A fixture that records what was actually asked of the venue."""

    def __init__(self, clock):
        super().__init__(clock=clock)
        self.calls: list[tuple[str, str, int]] = []
        self.book_calls = 0
        self.ticker_calls = 0

    async def get_ohlcv(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe.value, limit))
        return await super().get_ohlcv(symbol, timeframe, limit)

    async def get_order_book(self, symbol, depth=100):
        self.book_calls += 1
        return await super().get_order_book(symbol, depth)

    async def get_tickers_24h(self, symbols=None):
        self.ticker_calls += 1
        return await super().get_tickers_24h(symbols)


def _wrapped(clock, **kw):
    inner = _Counting(clock)
    return inner, wrap_with_cache(inner, clock=clock, **kw)


# --------------------------------------------------------------------------- #
# The property the whole file exists for
# --------------------------------------------------------------------------- #


async def test_a_cached_series_is_identical_to_an_uncached_one():
    clock = FrozenClock(FIXED_NOW_MS)
    inner, cached = _wrapped(clock)
    direct = await FixtureProvider(clock=clock).get_ohlcv("BTCUSDT", Timeframe.H1, 120)

    first = await cached.get_ohlcv("BTCUSDT", Timeframe.H1, 120)
    second = await cached.get_ohlcv("BTCUSDT", Timeframe.H1, 120)

    for got in (first, second):
        assert got.close.tolist() == direct.close.tolist()
        assert got.close_time_ms.tolist() == direct.close_time_ms.tolist()
        assert got.last_is_open == direct.last_is_open
    assert len(inner.calls) == 1  # and only one of them cost a request


# --------------------------------------------------------------------------- #
# Validity is the bar boundary, not a duration
# --------------------------------------------------------------------------- #


async def test_the_same_bar_is_served_from_cache():
    clock = FrozenClock(FIXED_NOW_MS)
    inner, cached = _wrapped(clock)

    await cached.get_ohlcv("BTCUSDT", Timeframe.H1, 100)
    clock.advance(60)  # still inside the same hourly bar
    await cached.get_ohlcv("BTCUSDT", Timeframe.H1, 100)

    assert len(inner.calls) == 1
    assert cached.cache_stats()["hits"] == 1


async def test_a_new_bar_closing_invalidates_the_entry():
    clock = FrozenClock(FIXED_NOW_MS)
    inner, cached = _wrapped(clock)

    await cached.get_ohlcv("BTCUSDT", Timeframe.H1, 100)
    clock.advance(Timeframe.H1.seconds + 1)  # a bar has closed since
    await cached.get_ohlcv("BTCUSDT", Timeframe.H1, 100)

    assert len(inner.calls) == 2


async def test_validity_is_the_next_bar_close_not_a_fixed_ttl():
    """Fetched one second before a bar closes, the entry expires one second later."""
    clock = FrozenClock(FIXED_NOW_MS)
    inner, cached = _wrapped(clock)
    tf = Timeframe.M5
    # Position the clock 1s before the current 5m bar closes.
    clock.set(((FIXED_NOW_MS // tf.ms) + 1) * tf.ms - 1000)

    await cached.get_ohlcv("BTCUSDT", tf, 100)
    clock.advance(2)
    await cached.get_ohlcv("BTCUSDT", tf, 100)

    assert len(inner.calls) == 2, "the bar closed, so the series must be re-read"


async def test_slow_timeframes_are_read_once_across_many_scans():
    """The reason this module exists: a daily bar changes once a day."""
    clock = FrozenClock(FIXED_NOW_MS)
    inner, cached = _wrapped(clock)

    for _ in range(20):
        await cached.get_ohlcv("BTCUSDT", Timeframe.D1, 400)
        clock.advance(60)

    assert len(inner.calls) == 1


# --------------------------------------------------------------------------- #
# Depth of the window
# --------------------------------------------------------------------------- #


async def test_a_shallower_request_is_served_as_a_tail_of_the_cached_window():
    clock = FrozenClock(FIXED_NOW_MS)
    inner, cached = _wrapped(clock)

    deep = await cached.get_ohlcv("BTCUSDT", Timeframe.H1, 200)
    shallow = await cached.get_ohlcv("BTCUSDT", Timeframe.H1, 50)

    assert len(inner.calls) == 1
    assert len(shallow) == 50
    # The tail of the deep window, not some other 50 bars.
    assert shallow.close_time_ms.tolist() == deep.close_time_ms[-50:].tolist()


async def test_a_deeper_request_is_a_miss_rather_than_a_truncated_answer():
    clock = FrozenClock(FIXED_NOW_MS)
    inner, cached = _wrapped(clock)

    await cached.get_ohlcv("BTCUSDT", Timeframe.H1, 50)
    deep = await cached.get_ohlcv("BTCUSDT", Timeframe.H1, 300)

    assert len(inner.calls) == 2
    assert len(deep) == 300


async def test_the_tail_slice_keeps_the_forming_candle_flag():
    clock = FrozenClock(FIXED_NOW_MS)
    _, cached = _wrapped(clock)

    full = await cached.get_ohlcv("BTCUSDT", Timeframe.H1, 200)
    tail = await cached.get_ohlcv("BTCUSDT", Timeframe.H1, 60)
    assert tail.last_is_open == full.last_is_open
    # closed() must therefore drop the same bar in both.
    assert tail.closed().last_close_time_ms == full.closed().last_close_time_ms


# --------------------------------------------------------------------------- #
# What must never be cached
# --------------------------------------------------------------------------- #


async def test_order_books_are_never_served_from_cache():
    """A depth snapshot is a statement about right now."""
    clock = FrozenClock(FIXED_NOW_MS)
    inner, cached = _wrapped(clock)

    await cached.get_order_book("BTCUSDT")
    await cached.get_order_book("BTCUSDT")
    assert inner.book_calls == 2


async def test_tickers_are_never_served_from_cache():
    clock = FrozenClock(FIXED_NOW_MS)
    inner, cached = _wrapped(clock)

    await cached.get_tickers_24h(["BTCUSDT"])
    await cached.get_tickers_24h(["BTCUSDT"])
    assert inner.ticker_calls == 2


# --------------------------------------------------------------------------- #
# Transparency: the wrapper must not change what the system believes it has
# --------------------------------------------------------------------------- #


async def test_the_wrapper_reports_the_venue_underneath():
    clock = FrozenClock(FIXED_NOW_MS)
    _, cached = _wrapped(clock)
    assert cached.name == "SYNTHETIC-FIXTURE"
    # Otherwise the synthetic-data banner would silently disappear.
    assert is_synthetic(cached) is True
    assert cached.reference_symbol == "BTCUSDT"


async def test_order_book_capability_is_preserved_through_the_wrapper():
    """The scanner decides whether to spend a pass on depth with isinstance."""
    clock = FrozenClock(FIXED_NOW_MS)
    _, cached = _wrapped(clock)
    assert isinstance(cached, OrderBookProvider)


def test_doctor_gets_an_uncached_provider():
    """Proving the live API works cannot be done against a cache."""
    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    assert isinstance(build_market_provider(settings), CachingMarketDataProvider)
    assert not isinstance(build_market_provider(settings, use_cache=False), CachingMarketDataProvider)


def test_the_cache_can_be_turned_off_entirely():
    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.providers.cache_enabled = False
    assert not isinstance(build_market_provider(settings), CachingMarketDataProvider)


# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #


async def test_entries_are_evicted_least_recently_used_first():
    clock = FrozenClock(FIXED_NOW_MS)
    inner, cached = _wrapped(clock, max_entries=2)

    await cached.get_ohlcv("BTCUSDT", Timeframe.H1, 60)
    await cached.get_ohlcv("ETHUSDT", Timeframe.H1, 60)
    await cached.get_ohlcv("BTCUSDT", Timeframe.H1, 60)  # BTC is now the newest
    await cached.get_ohlcv("SOLUSDT", Timeframe.H1, 60)  # evicts ETH

    before = len(inner.calls)
    await cached.get_ohlcv("BTCUSDT", Timeframe.H1, 60)
    assert len(inner.calls) == before, "BTC should still be cached"
    await cached.get_ohlcv("ETHUSDT", Timeframe.H1, 60)
    assert len(inner.calls) == before + 1, "ETH was the least recently used"
    assert cached.cache_stats()["evictions"] >= 1


async def test_invalidate_drops_one_symbol_or_everything():
    clock = FrozenClock(FIXED_NOW_MS)
    inner, cached = _wrapped(clock)

    await cached.get_ohlcv("BTCUSDT", Timeframe.H1, 60)
    await cached.get_ohlcv("ETHUSDT", Timeframe.H1, 60)
    assert cached.invalidate("BTCUSDT") == 1
    assert cached.cache_stats()["entries"] == 1
    assert cached.invalidate() == 1
    assert cached.cache_stats()["entries"] == 0


@pytest.mark.parametrize("timeframe", [Timeframe.M5, Timeframe.H1, Timeframe.D1])
async def test_the_saving_grows_with_the_timeframe(timeframe):
    """Ten one-minute scans: the slower the bar, the fewer the requests."""
    clock = FrozenClock(FIXED_NOW_MS)
    inner, cached = _wrapped(clock)
    for _ in range(10):
        await cached.get_ohlcv("BTCUSDT", timeframe, 100)
        clock.advance(60)

    expected_ceiling = {Timeframe.M5: 3, Timeframe.H1: 1, Timeframe.D1: 1}[timeframe]
    assert len(inner.calls) <= expected_ceiling
