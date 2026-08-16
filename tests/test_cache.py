"""Candle cache: does it save requests without changing a single number?

The saving is easy. The correctness is the point, and it rests on one property —
a closed candle is immutable, so serving it from memory is not an approximation
of the network answer but the same answer.

Three things would break that property, and each has a test here: storing the
still-forming candle, serving past a bar boundary, and letting cached data look
fresher than it is. A fourth test is arguably the most important of all: a full
scan with the cache must produce output identical to a scan without it, field by
field. If that ever fails, the cache is not an optimisation — it is a bug.
"""

from __future__ import annotations

import numpy as np
import pytest

from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.core.types import Timeframe
from cryptopulse.providers.cache import (
    PUBLISH_LAG_MS,
    CachedMarketDataProvider,
    CandleCache,
)
from cryptopulse.providers.fixture import FixtureProvider
from cryptopulse.providers.registry import build_market_provider, is_synthetic
from cryptopulse.scanner.cex import CexScanner
from cryptopulse.scanner.memory import ScoreMemory
from tests.conftest import FIXED_NOW_MS

SYM = "SOLUSDT"


class CountingFixture(FixtureProvider):
    """Fixture provider that records how many network-shaped calls it served."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.kline_calls = 0
        self.ticker_calls = 0
        self.book_calls = 0

    async def get_ohlcv(self, symbol, timeframe, limit, **kw):
        self.kline_calls += 1
        return await super().get_ohlcv(symbol, timeframe, limit, **kw)

    async def get_tickers_24h(self, symbols=None, **kw):
        self.ticker_calls += 1
        return await super().get_tickers_24h(symbols, **kw)

    async def get_order_book(self, symbol, depth=100, **kw):
        self.book_calls += 1
        return await super().get_order_book(symbol, depth=depth, **kw)


def _cached(now_ms: int = FIXED_NOW_MS) -> CachedMarketDataProvider:
    clock = FrozenClock(now_ms)
    return CachedMarketDataProvider(CountingFixture(clock=clock), clock=clock)


# --------------------------------------------------------------------------- #
# The saving
# --------------------------------------------------------------------------- #


async def test_a_second_request_inside_the_same_bar_costs_nothing():
    p = _cached()
    await p.get_ohlcv(SYM, Timeframe.M5, 200)
    await p.get_ohlcv(SYM, Timeframe.M5, 200)
    await p.get_ohlcv(SYM, Timeframe.M5, 200)

    assert p.inner.kline_calls == 1, "only the first request should reach the provider"
    assert p.cache_stats()["hits"] == 2
    assert p.cache_stats()["requests_saved"] == 2


async def test_the_cached_answer_is_byte_for_byte_the_network_answer():
    p = _cached()
    first = await p.get_ohlcv(SYM, Timeframe.M5, 200)
    second = await p.get_ohlcv(SYM, Timeframe.M5, 200)

    assert p.inner.kline_calls == 1
    for f in ("open_time_ms", "open", "high", "low", "close", "volume", "close_time_ms"):
        np.testing.assert_array_equal(
            getattr(first, f), getattr(second, f), err_msg=f"{f} differs between network and cache"
        )


async def test_each_symbol_and_timeframe_is_cached_separately():
    p = _cached()
    await p.get_ohlcv(SYM, Timeframe.M5, 100)
    await p.get_ohlcv(SYM, Timeframe.H1, 100)
    await p.get_ohlcv("BTCUSDT", Timeframe.M5, 100)
    assert p.inner.kline_calls == 3

    await p.get_ohlcv(SYM, Timeframe.M5, 100)
    assert p.inner.kline_calls == 3, "the three keys must not collide"


# --------------------------------------------------------------------------- #
# The boundary: stale data must never be served
# --------------------------------------------------------------------------- #


async def test_the_cache_is_refused_once_a_new_candle_has_closed():
    clock = FrozenClock(FIXED_NOW_MS)
    p = CachedMarketDataProvider(CountingFixture(clock=clock), clock=clock)

    await p.get_ohlcv(SYM, Timeframe.M5, 100)
    assert p.inner.kline_calls == 1

    # Still inside the same bar: served from memory.
    clock.advance(60)
    await p.get_ohlcv(SYM, Timeframe.M5, 100)
    assert p.inner.kline_calls == 1

    # Past the next boundary: a real request must be made.
    clock.advance(Timeframe.M5.seconds + PUBLISH_LAG_MS / 1000)
    await p.get_ohlcv(SYM, Timeframe.M5, 100)
    assert p.inner.kline_calls == 2, "a newly closed candle must not be missed"


async def test_a_long_timeframe_survives_many_scans_before_refetching():
    """The measured redundancy: a 4h candle was being refetched 240 times."""
    clock = FrozenClock(FIXED_NOW_MS)
    p = CachedMarketDataProvider(CountingFixture(clock=clock), clock=clock)

    for _ in range(200):  # 200 scans one minute apart
        await p.get_ohlcv(SYM, Timeframe.H4, 100)
        clock.advance(60)

    assert p.inner.kline_calls <= 2, f"expected ~1 fetch over 200 minutes, got {p.inner.kline_calls}"
    assert p.cache_stats()["hit_rate"] > 0.98


async def test_each_requested_depth_is_cached_separately():
    """A 300-bar fetch yields 299 closed bars; slicing its tail to answer a
    100-bar request would return a window one bar longer than the network would
    have. Keying on the limit removes the question entirely."""
    p = _cached()
    deep = await p.get_ohlcv(SYM, Timeframe.M5, 300)
    shallow = await p.get_ohlcv(SYM, Timeframe.M5, 100)
    assert p.inner.kline_calls == 2

    # Each depth then answers from its own entry.
    again_deep = await p.get_ohlcv(SYM, Timeframe.M5, 300)
    again_shallow = await p.get_ohlcv(SYM, Timeframe.M5, 100)
    assert p.inner.kline_calls == 2
    assert len(again_deep) == len(deep)
    assert len(again_shallow) == len(shallow)


# --------------------------------------------------------------------------- #
# The forming candle must never enter the cache
# --------------------------------------------------------------------------- #


async def test_the_still_forming_candle_is_never_stored():
    """The whole anti-look-ahead guarantee, one layer lower. A candle whose
    high/low/close can still move must not be handed out from memory."""
    clock = FrozenClock(FIXED_NOW_MS)
    p = CachedMarketDataProvider(CountingFixture(clock=clock), clock=clock)

    raw = await p.inner.get_ohlcv(SYM, Timeframe.M5, 100)
    served = await p.get_ohlcv(SYM, Timeframe.M5, 100)

    assert served.last_close_time_ms <= clock.now_ms(), "a forming candle was served"
    if raw.last_is_open:
        assert len(served) == len(raw) - 1
    np.testing.assert_array_equal(served.close_time_ms, raw.closed().close_time_ms)


async def test_the_cache_returns_the_same_shape_on_a_hit_and_on_a_miss():
    """A caller must not be able to tell which path answered — otherwise the
    presence of an extra forming candle would depend on cache state."""
    clock = FrozenClock(FIXED_NOW_MS)
    p = CachedMarketDataProvider(CountingFixture(clock=clock), clock=clock)

    miss = await p.get_ohlcv(SYM, Timeframe.M5, 120)
    hit = await p.get_ohlcv(SYM, Timeframe.M5, 120)
    assert len(miss) == len(hit)
    assert miss.last_close_time_ms == hit.last_close_time_ms


async def _seconds_until_the_bar_rolls(clock, timeframe: Timeframe) -> float:
    """How long the current bar still has to run, from the data itself.

    Tests must not assume where a frozen clock happens to sit inside a candle:
    that would make them pass or fail depending on the chosen timestamp rather
    than on the cache's behaviour.
    """
    probe = FixtureProvider(clock=clock)
    series = (await probe.get_ohlcv(SYM, timeframe, 10)).closed()
    next_boundary = series.last_close_time_ms + timeframe.ms + PUBLISH_LAG_MS
    return (next_boundary - clock.now_ms()) / 1000


async def test_cached_data_ages_instead_of_looking_permanently_fresh():
    """Staleness is derived from each candle's own close time, so a cached
    series gets older on its own. Nothing here can make old data look new."""
    clock = FrozenClock(FIXED_NOW_MS)
    p = CachedMarketDataProvider(CountingFixture(clock=clock), clock=clock)

    served = await p.get_ohlcv(SYM, Timeframe.H4, 100)
    age_now = clock.now_ms() - served.last_close_time_ms

    # Stay inside the current bar, wherever the clock happens to sit in it.
    wait = max(60.0, (await _seconds_until_the_bar_rolls(clock, Timeframe.H4)) / 2)
    clock.advance(wait)

    again = await p.get_ohlcv(SYM, Timeframe.H4, 100)
    age_later = clock.now_ms() - again.last_close_time_ms

    assert p.inner.kline_calls == 1, "still the cached entry"
    assert age_later > age_now, "a cached series must get older, not stay fresh"
    assert age_later - age_now == pytest.approx(wait * 1000, abs=1000)


# --------------------------------------------------------------------------- #
# What is deliberately not cached
# --------------------------------------------------------------------------- #


async def test_order_books_are_never_cached():
    p = _cached()
    await p.get_order_book(SYM, 50)
    await p.get_order_book(SYM, 50)
    assert p.inner.book_calls == 2, "depth is the freshest data here and must never be reused"


async def test_tickers_are_never_cached():
    p = _cached()
    await p.get_tickers_24h()
    await p.get_tickers_24h()
    assert p.inner.ticker_calls == 2


def test_doctor_gets_an_uncached_provider():
    """A diagnostic answered from memory would report success without having
    contacted anything."""
    s = CryptoPulseSettings()
    s.providers.market_data = "fixture"
    assert isinstance(build_market_provider(s), CachedMarketDataProvider)
    assert not isinstance(build_market_provider(s, cached=False), CachedMarketDataProvider)


def test_the_cache_can_be_switched_off_by_configuration():
    s = CryptoPulseSettings()
    s.providers.market_data = "fixture"
    s.providers.candle_cache = False
    assert not isinstance(build_market_provider(s), CachedMarketDataProvider)


# --------------------------------------------------------------------------- #
# The wrapper must be invisible
# --------------------------------------------------------------------------- #


def test_the_wrapper_keeps_the_provider_identity_so_the_demo_banner_survives():
    """The regression this would otherwise cause is severe: `is_synthetic`
    identifies generated data by the provider's name. A wrapper shadowing it
    would make a fixture-backed scan report itself LIVE and drop the banner."""
    s = CryptoPulseSettings()
    s.providers.market_data = "fixture"
    wrapped = build_market_provider(s)

    assert wrapped.name == "SYNTHETIC-FIXTURE"
    assert is_synthetic(wrapped) is True
    assert wrapped.reference_symbol == FixtureProvider(clock=FrozenClock(FIXED_NOW_MS)).reference_symbol


async def test_health_and_symbols_are_forwarded():
    p = _cached()
    assert (await p.health()).available is True
    assert len(await p.list_symbols("USDT")) > 0


# --------------------------------------------------------------------------- #
# Bounded memory
# --------------------------------------------------------------------------- #


async def test_the_cache_is_bounded_and_evicts_least_recently_used():
    """This runs on a phone. An unbounded cache would grow with every symbol
    ever scanned."""
    clock = FrozenClock(FIXED_NOW_MS)
    cache = CandleCache(max_entries=3, clock=clock)
    provider = FixtureProvider(clock=clock)

    for sym in ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"):
        cache.put(sym, Timeframe.M5, 50, await provider.get_ohlcv(SYM, Timeframe.M5, 50))

    assert cache.stats.stored_series == 3
    assert cache.stats.evictions == 1
    assert cache.get("AAAUSDT", Timeframe.M5, 50) is None, "the oldest entry should be gone"
    assert cache.get("DDDUSDT", Timeframe.M5, 50) is not None


def test_an_unused_cache_reports_no_hit_rate_rather_than_zero():
    stats = CandleCache().stats
    assert stats.hit_rate is None
    assert stats.to_dict()["hit_rate"] is None


# --------------------------------------------------------------------------- #
# The test that matters most
# --------------------------------------------------------------------------- #


async def test_a_scan_with_the_cache_is_identical_to_a_scan_without_it():
    """If this ever fails, the cache is not an optimisation but a bug.

    Same frozen instant, same generated history, one scanner reading through the
    cache and one reading directly. Every score, every component, every state
    must match — not approximately, exactly.
    """
    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.scanner.min_quote_volume_24h = 100_000.0

    clock = FrozenClock(FIXED_NOW_MS)
    direct = CexScanner(settings, provider=FixtureProvider(clock=clock), memory=ScoreMemory(), clock=clock)
    cached = CexScanner(
        settings,
        provider=CachedMarketDataProvider(FixtureProvider(clock=clock), clock=clock),
        memory=ScoreMemory(),
        clock=clock,
    )

    a = await direct.scan()
    b = await cached.scan()

    assert a.succeeded == b.succeeded > 5
    assert [r.symbol for r in a.results] == [r.symbol for r in b.results], "ranking changed"

    for ra, rb in zip(a.results, b.results, strict=True):
        assert ra.final_score == pytest.approx(rb.final_score, abs=1e-9), ra.symbol
        assert ra.raw_score == pytest.approx(rb.raw_score, abs=1e-9), ra.symbol
        assert ra.risk_penalty == pytest.approx(rb.risk_penalty, abs=1e-9), ra.symbol
        assert ra.state.state == rb.state.state, ra.symbol
        assert ra.maturity.score == pytest.approx(rb.maturity.score, abs=1e-9), ra.symbol
        assert ra.confidence.score == pytest.approx(rb.confidence.score, abs=1e-9), ra.symbol
        assert ra.price == pytest.approx(rb.price, abs=1e-12), ra.symbol
        for ca, cb in zip(ra.components, rb.components, strict=True):
            assert ca.name == cb.name
            assert ca.points == pytest.approx(cb.points, abs=1e-9), f"{ra.symbol}/{ca.name}"


async def test_a_second_scan_inside_the_same_bar_fetches_nothing():
    """The operational point: the second scan of a minute-long loop should cost
    no kline requests at all.

    Counting is done on the cache's own statistics rather than on calls reaching
    the inner provider, because the *synthetic* provider builds its tickers and
    order books from its own candles — those internal calls are an artifact of
    the fixture and have no equivalent against a real venue.
    """
    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.scanner.min_quote_volume_24h = 100_000.0

    clock = FrozenClock(FIXED_NOW_MS)
    provider = CachedMarketDataProvider(FixtureProvider(clock=clock), clock=clock)
    scanner = CexScanner(settings, provider=provider, memory=ScoreMemory(), clock=clock)

    await scanner.scan()
    first = provider.cache.stats
    fetched = first.misses
    assert fetched > 50, "the first scan must genuinely fetch"
    assert first.hits == 0

    # Stay inside every bar the scan reads: a 15m candle can roll while the 5m
    # one still has minutes left, and that refetch would be correct.
    margins = [await _seconds_until_the_bar_rolls(clock, tf) for tf in settings.scanner.timeframes]
    clock.advance(max(1.0, min(margins) / 2))

    await scanner.scan()
    assert provider.cache.stats.misses == fetched, "a scan inside the same bar refetched candles"
    assert provider.cache.stats.hits >= fetched, "the second scan should have been served entirely from memory"
    assert provider.cache.stats.hit_rate >= 0.5


async def test_the_measured_saving_over_a_realistic_loop():
    """Ten scans a minute apart, the shape of the real loop. The audit measured
    92.8 % of kline requests as redundant; this asserts the cache removes them."""
    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.scanner.min_quote_volume_24h = 100_000.0

    clock = FrozenClock(FIXED_NOW_MS)
    provider = CachedMarketDataProvider(FixtureProvider(clock=clock), clock=clock)
    scanner = CexScanner(settings, provider=provider, memory=ScoreMemory(), clock=clock)

    for _ in range(10):
        await scanner.scan()
        clock.advance(60)

    stats = provider.cache.stats
    assert stats.hit_rate > 0.6, f"expected most requests served from memory, got {stats.hit_rate:.1%}"
