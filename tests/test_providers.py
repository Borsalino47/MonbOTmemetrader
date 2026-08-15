"""Provider contract, HTTP resilience, and the honest handling of failure."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from cryptopulse.config.settings import ProviderSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.core.errors import CircuitOpen, DataUnavailable, RateLimited, SourceUnavailable
from cryptopulse.core.types import Timeframe
from cryptopulse.providers.binance import BinanceSpotProvider
from cryptopulse.providers.fixture import SYNTHETIC_SOURCE, FixtureProvider
from cryptopulse.providers.http import CircuitBreaker, HttpClient, WeightedRateLimiter
from tests.conftest import FIXED_NOW_MS

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #


async def test_rate_limiter_allows_traffic_inside_the_budget():
    limiter = WeightedRateLimiter(weight_per_minute=100)
    for _ in range(10):
        await limiter.acquire(5)
    assert limiter.used == 50
    assert limiter.remaining_pct() == pytest.approx(50.0)


async def test_rate_limiter_blocks_once_the_budget_is_spent():
    limiter = WeightedRateLimiter(weight_per_minute=10, window_seconds=30)
    await limiter.acquire(10)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(limiter.acquire(5), timeout=0.25)


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #


async def test_circuit_opens_after_the_threshold_and_recovers():
    cb = CircuitBreaker(failure_threshold=3, reset_seconds=0.2)
    assert not cb.is_open
    for _ in range(3):
        cb.record_failure()
    assert cb.is_open
    await asyncio.sleep(0.25)
    assert not cb.is_open, "circuit should go half-open so a probe can run"
    cb.record_success()
    assert not cb.is_open


async def test_open_circuit_short_circuits_the_request():
    client = HttpClient("https://example.invalid", name="test", circuit_failure_threshold=1)
    client.breaker.record_failure()
    with pytest.raises(CircuitOpen):
        await client.get_json("/whatever")
    await client.close()


# --------------------------------------------------------------------------- #
# HTTP behaviour via a mock transport
# --------------------------------------------------------------------------- #


def _client_with(handler, **kwargs) -> HttpClient:
    client = HttpClient("https://mock.test", name="mock", retry_base_delay=0.001, **kwargs)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


async def test_server_error_is_retried_then_raises_source_unavailable():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="upstream down")

    client = _client_with(handler, max_retries=2)
    with pytest.raises(SourceUnavailable):
        await client.get_json("/x")
    assert calls["n"] == 3, "should have made the original call plus two retries"
    await client.close()


async def test_client_error_is_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, text='{"code":-1121,"msg":"Invalid symbol."}')

    client = _client_with(handler, max_retries=3)
    with pytest.raises(SourceUnavailable):
        await client.get_json("/x")
    assert calls["n"] == 1, "a 4xx is our bug; retrying it just wastes the rate limit"
    await client.close()


async def test_rate_limit_response_raises_rate_limited():
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "0"})

    client = _client_with(handler, max_retries=1)
    with pytest.raises(RateLimited):
        await client.get_json("/x")
    await client.close()


async def test_transient_failure_then_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"ok": True})

    client = _client_with(handler, max_retries=2)
    assert await client.get_json("/x") == {"ok": True}
    await client.close()


# --------------------------------------------------------------------------- #
# Binance parsing (against mocked payloads — the live check lives in `doctor`)
# --------------------------------------------------------------------------- #


def _binance_with(handler) -> BinanceSpotProvider:
    provider = BinanceSpotProvider(ProviderSettings(), clock=FrozenClock(FIXED_NOW_MS))
    provider.http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider.http.retry_base_delay = 0.001
    return provider


async def test_binance_kline_field_mapping():
    """Guards the documented column order the whole connector depends on."""
    tf_ms = Timeframe.M5.ms
    open_time = (FIXED_NOW_MS // tf_ms) * tf_ms - tf_ms * 2
    rows = [
        [open_time + i * tf_ms, "100.0", "105.0", "99.0", "104.0", "1234.5",
         open_time + (i + 1) * tf_ms - 1, "128000.0", 321, "600.0", "62000.0", "0"]
        for i in range(3)
    ]

    provider = _binance_with(lambda r: httpx.Response(200, json=rows))
    series = await provider.get_ohlcv("BTCUSDT", Timeframe.M5, 3)

    assert series.open[0] == 100.0
    assert series.high[0] == 105.0
    assert series.low[0] == 99.0
    assert series.close[0] == 104.0
    assert series.volume[0] == 1234.5
    assert series.quote_volume[0] == 128000.0
    assert series.trades[0] == 321
    await provider.close()


async def test_binance_marks_the_unfinished_candle_as_open():
    tf_ms = Timeframe.M5.ms
    current_open = (FIXED_NOW_MS // tf_ms) * tf_ms
    rows = [
        # a finished candle
        [current_open - tf_ms, "1", "2", "0.5", "1.5", "10", current_open - 1, "15", 5, "5", "7", "0"],
        # the candle in progress: its close_time is in the future
        [current_open, "1.5", "2.5", "1.4", "2.0", "4", current_open + tf_ms - 1, "8", 2, "2", "4", "0"],
    ]
    provider = _binance_with(lambda r: httpx.Response(200, json=rows))
    series = await provider.get_ohlcv("BTCUSDT", Timeframe.M5, 2)

    assert series.last_is_open is True
    assert len(series) == 2
    assert len(series.closed()) == 1
    assert series.closed().last_close == 1.5
    await provider.close()


async def test_binance_skips_unparsable_rows_without_failing_the_batch():
    tf_ms = Timeframe.M5.ms
    base = (FIXED_NOW_MS // tf_ms) * tf_ms - tf_ms * 3
    rows = [
        [base, "1", "2", "0.5", "1.5", "10", base + tf_ms - 1, "15", 5, "5", "7", "0"],
        ["garbage"],  # malformed
        [base + 2 * tf_ms, "2", "3", "1.5", "2.5", "12", base + 3 * tf_ms - 1, "30", 6, "6", "8", "0"],
    ]
    provider = _binance_with(lambda r: httpx.Response(200, json=rows))
    series = await provider.get_ohlcv("BTCUSDT", Timeframe.M5, 3)
    assert len(series) == 2, "one bad row must not discard the good ones"
    await provider.close()


async def test_binance_empty_response_raises_data_unavailable():
    provider = _binance_with(lambda r: httpx.Response(200, json=[]))
    with pytest.raises(DataUnavailable):
        await provider.get_ohlcv("NOSUCHUSDT", Timeframe.M5, 10)
    await provider.close()


async def test_binance_health_reports_unavailable_rather_than_raising():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    provider = _binance_with(handler)
    health = await provider.health()
    assert health.available is False
    assert health.to_dict()["status"] == "SOURCE_UNAVAILABLE"
    await provider.close()


async def test_binance_gaps_are_flagged_as_partial():
    from cryptopulse.core.types import DataQuality

    tf_ms = Timeframe.M5.ms
    base = (FIXED_NOW_MS // tf_ms) * tf_ms - tf_ms * 10
    rows = []
    for i in (0, 1, 5):  # a deliberate hole between bar 1 and bar 5
        t = base + i * tf_ms
        rows.append([t, "1", "2", "0.5", "1.5", "10", t + tf_ms - 1, "15", 5, "5", "7", "0"])
    provider = _binance_with(lambda r: httpx.Response(200, json=rows))
    series = await provider.get_ohlcv("BTCUSDT", Timeframe.M5, 3)
    assert series.has_gaps()
    assert series.provenance.quality is DataQuality.PARTIAL
    assert series.provenance.note == "GAPS"
    await provider.close()


async def test_binance_depth_parsing_and_imbalance_sign():
    payload = {
        "lastUpdateId": 1,
        "bids": [["100.0", "10"], ["99.9", "5"]],
        "asks": [["100.1", "2"], ["100.2", "1"]],
    }
    provider = _binance_with(lambda r: httpx.Response(200, json=payload))
    book = await provider.get_order_book("BTCUSDT", 50)
    assert book.best_bid == 100.0 and book.best_ask == 100.1
    assert book.spread_bps is not None and book.spread_bps > 0
    assert book.imbalance(0.01) > 0, "far more bid size than ask size should read bullish"
    await provider.close()


# --------------------------------------------------------------------------- #
# Fixture provider must be unmistakably synthetic
# --------------------------------------------------------------------------- #


async def test_fixture_provider_labels_everything_synthetic():
    from cryptopulse.core.types import DataQuality

    provider = FixtureProvider(clock=FrozenClock(FIXED_NOW_MS))
    series = await provider.get_ohlcv("BTCUSDT", Timeframe.M5, 100)
    assert provider.name == SYNTHETIC_SOURCE
    assert series.provenance.source == SYNTHETIC_SOURCE
    assert series.provenance.quality is DataQuality.PARTIAL
    assert "NOT_REAL_MARKET" in series.provenance.note

    health = await provider.health()
    assert "SYNTHETIC" in (health.detail or "")


async def test_fixture_provider_is_deterministic():
    a = FixtureProvider(clock=FrozenClock(FIXED_NOW_MS))
    b = FixtureProvider(clock=FrozenClock(FIXED_NOW_MS))
    s1 = await a.get_ohlcv("SOLUSDT", Timeframe.M5, 200)
    s2 = await b.get_ohlcv("SOLUSDT", Timeframe.M5, 200)
    assert (s1.close == s2.close).all()


async def test_fixture_provider_rejects_unknown_symbols():
    provider = FixtureProvider(clock=FrozenClock(FIXED_NOW_MS))
    with pytest.raises(DataUnavailable):
        await provider.get_ohlcv("NOTREAL", Timeframe.M5, 50)


async def test_fixture_ohlc_invariants_hold():
    provider = FixtureProvider(clock=FrozenClock(FIXED_NOW_MS))
    s = await provider.get_ohlcv("ETHUSDT", Timeframe.M15, 300)
    assert (s.high >= s.low).all()
    assert (s.high >= s.open).all() and (s.high >= s.close).all()
    assert (s.low <= s.open).all() and (s.low <= s.close).all()
    assert (s.volume > 0).all()
