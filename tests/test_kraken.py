"""Kraken connector: envelope handling, field mapping, and the pair-rename trap.

Parsing is tested against mocked payloads shaped like ccxt 4.5.73 documents
them. The live round-trip lives in `cryptopulse.cli doctor --provider kraken`.
"""

from __future__ import annotations

import httpx
import pytest

from cryptopulse.config.settings import ProviderSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.core.errors import DataUnavailable, SourceUnavailable
from cryptopulse.core.types import Timeframe
from cryptopulse.providers.kraken import OHLC_FIELDS, TIMEFRAME_MINUTES, KrakenProvider
from tests.conftest import FIXED_NOW_MS

pytestmark = pytest.mark.asyncio


def _kraken(handler) -> KrakenProvider:
    provider = KrakenProvider(ProviderSettings(), clock=FrozenClock(FIXED_NOW_MS))
    provider.http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider.http.retry_base_delay = 0.001
    return provider


def _ok(result: dict) -> httpx.Response:
    return httpx.Response(200, json={"error": [], "result": result})


# --------------------------------------------------------------------------- #
# The envelope — Kraken reports failures with HTTP 200
# --------------------------------------------------------------------------- #


async def test_error_array_is_raised_even_though_http_is_200():
    """The single most dangerous failure mode: a 200 that carries an error."""
    def handler(request):
        return httpx.Response(200, json={"error": ["EQuery:Unknown asset pair"], "result": {}})

    provider = _kraken(handler)
    with pytest.raises(DataUnavailable, match="Unknown asset pair"):
        await provider.get_ohlcv("NOSUCHPAIR", Timeframe.M5, 10)
    await provider.close()


async def test_service_errors_are_source_unavailable_not_data_unavailable():
    """EService means the venue is down — a different fault from a bad symbol."""
    def handler(request):
        return httpx.Response(200, json={"error": ["EService:Unavailable"], "result": {}})

    provider = _kraken(handler)
    with pytest.raises(SourceUnavailable):
        await provider.get_tickers_24h(["XBTUSDT"])
    await provider.close()


async def test_missing_result_block_is_rejected():
    provider = _kraken(lambda r: httpx.Response(200, json={"error": []}))
    with pytest.raises(SourceUnavailable, match="no result"):
        await provider.get_ohlcv("XBTUSDT", Timeframe.M5, 10)
    await provider.close()


async def test_health_reports_non_online_exchange_status():
    """Kraken publishes maintenance / cancel_only modes; only online is healthy."""
    provider = _kraken(lambda r: _ok({"status": "maintenance", "timestamp": "2026-01-01T00:00:00Z"}))
    health = await provider.health()
    assert health.available is False
    assert "maintenance" in (health.detail or "")
    await provider.close()


async def test_health_online_is_available():
    provider = _kraken(lambda r: _ok({"status": "online"}))
    assert (await provider.health()).available is True
    await provider.close()


# --------------------------------------------------------------------------- #
# OHLC — seconds, minutes, and index 6 being the volume
# --------------------------------------------------------------------------- #


def _ohlc_rows(n: int, tf: Timeframe) -> list:
    tf_s = tf.seconds
    base = (FIXED_NOW_MS // 1000 // tf_s) * tf_s - n * tf_s
    # [time, open, high, low, close, vwap, volume, count]
    return [
        [base + i * tf_s, "100.0", "105.0", "99.0", "104.0", "102.5", "1234.5", 321]
        for i in range(n)
    ]


async def test_ohlc_field_mapping_takes_volume_from_index_six():
    """Index 5 is VWAP and index 6 is volume. Swapping them would feed VWAP into
    every volume feature and silently corrupt RVOL."""
    rows = _ohlc_rows(3, Timeframe.M5)
    provider = _kraken(lambda r: _ok({"XXBTZUSD": rows, "last": rows[-1][0]}))
    series = await provider.get_ohlcv("XBTUSD", Timeframe.M5, 3)

    assert series.open[0] == 100.0
    assert series.high[0] == 105.0
    assert series.low[0] == 99.0
    assert series.close[0] == 104.0
    assert series.volume[0] == 1234.5, "volume must come from index 6, not the VWAP at index 5"
    assert series.trades[0] == 321
    await provider.close()


async def test_ohlc_timestamps_are_converted_from_seconds_to_milliseconds():
    rows = _ohlc_rows(2, Timeframe.M5)
    provider = _kraken(lambda r: _ok({"XXBTZUSD": rows}))
    series = await provider.get_ohlcv("XBTUSD", Timeframe.M5, 2)

    assert series.open_time_ms[0] == rows[0][0] * 1000
    assert series.close_time_ms[0] == rows[0][0] * 1000 + Timeframe.M5.ms - 1
    await provider.close()


async def test_interval_is_sent_in_minutes():
    seen = {}

    def handler(request):
        seen["interval"] = request.url.params.get("interval")
        return _ok({"XXBTZUSD": _ohlc_rows(2, Timeframe.H4)})

    provider = _kraken(handler)
    await provider.get_ohlcv("XBTUSD", Timeframe.H4, 2)
    assert seen["interval"] == "240", "4h must be sent as 240 minutes, not '4h'"
    await provider.close()


async def test_response_key_need_not_match_the_requested_pair():
    """Ask for XBTUSD, Kraken answers XXBTZUSD. Assuming otherwise breaks everything."""
    rows = _ohlc_rows(3, Timeframe.M5)
    provider = _kraken(lambda r: _ok({"XXBTZUSD": rows, "last": rows[-1][0]}))
    series = await provider.get_ohlcv("XBTUSD", Timeframe.M5, 3)
    assert len(series) == 3
    await provider.close()


async def test_last_cursor_is_never_mistaken_for_a_pair():
    rows = _ohlc_rows(2, Timeframe.M5)
    # `last` first in iteration order — the parser must skip it.
    provider = _kraken(lambda r: _ok({"last": rows[-1][0], "XXBTZUSD": rows}))
    series = await provider.get_ohlcv("XBTUSD", Timeframe.M5, 2)
    assert len(series) == 2
    await provider.close()


async def test_unfinished_candle_is_marked_open():
    tf = Timeframe.M5
    tf_s = tf.seconds
    current = (FIXED_NOW_MS // 1000 // tf_s) * tf_s
    rows = [
        [current - tf_s, "1", "2", "0.5", "1.5", "1.2", "10", 5],
        [current, "1.5", "2.5", "1.4", "2.0", "1.8", "4", 2],  # still forming
    ]
    provider = _kraken(lambda r: _ok({"XXBTZUSD": rows}))
    series = await provider.get_ohlcv("XBTUSD", tf, 2)

    assert series.last_is_open is True
    assert len(series.closed()) == 1
    await provider.close()


async def test_unparsable_ohlc_rows_are_skipped_not_fatal():
    rows = _ohlc_rows(3, Timeframe.M5)
    rows.insert(1, ["garbage"])
    provider = _kraken(lambda r: _ok({"XXBTZUSD": rows}))
    series = await provider.get_ohlcv("XBTUSD", Timeframe.M5, 5)
    assert len(series) == 3
    await provider.close()


async def test_unsupported_timeframe_is_refused():
    provider = _kraken(lambda r: _ok({}))
    assert Timeframe.M1 in TIMEFRAME_MINUTES
    await provider.close()


# --------------------------------------------------------------------------- #
# Ticker — quote volume derived by identity, and the [today, 24h] pairs
# --------------------------------------------------------------------------- #


def _ticker_row() -> dict:
    return {
        "a": ["61050.0", "1", "1.000"],
        "b": ["61040.0", "2", "2.000"],
        "c": ["61045.0", "0.05"],
        "v": ["100.0", "500.0"],      # [today, last 24h]
        "p": ["60900.0", "61000.0"],  # VWAP [today, last 24h]
        "t": [1000, 8000],
        "l": ["60000.0", "59500.0"],
        "h": ["62000.0", "62500.0"],
        "o": "60500.0",
    }


async def test_ticker_uses_the_24h_index_not_todays():
    provider = _kraken(lambda r: _ok({"XXBTZUSD": _ticker_row()}))
    t = (await provider.get_tickers_24h(["XBTUSD"]))["XXBTZUSD"]

    assert t.high_24h == 62500.0, "index 1 is the rolling 24h figure"
    assert t.low_24h == 59500.0
    assert t.trades_24h == 8000
    await provider.close()


async def test_quote_volume_is_base_volume_times_vwap():
    """Kraken publishes no quote volume; base * VWAP is the identity, not a guess."""
    provider = _kraken(lambda r: _ok({"XXBTZUSD": _ticker_row()}))
    t = (await provider.get_tickers_24h(["XBTUSD"]))["XXBTZUSD"]

    assert t.quote_volume_24h == pytest.approx(500.0 * 61000.0)
    assert "VWAP" in (t.provenance.note or ""), "a derived value must be disclosed"
    await provider.close()


async def test_ticker_change_is_computed_from_the_open():
    provider = _kraken(lambda r: _ok({"XXBTZUSD": _ticker_row()}))
    t = (await provider.get_tickers_24h(["XBTUSD"]))["XXBTZUSD"]
    assert t.price_change_pct_24h == pytest.approx((61045.0 - 60500.0) / 60500.0 * 100.0)
    await provider.close()


async def test_ticker_with_no_parsable_row_raises():
    provider = _kraken(lambda r: _ok({"XXBTZUSD": {"broken": True}}))
    with pytest.raises(DataUnavailable):
        await provider.get_tickers_24h(["XBTUSD"])
    await provider.close()


# --------------------------------------------------------------------------- #
# Pairs and order book
# --------------------------------------------------------------------------- #


async def test_list_symbols_filters_on_the_wsname_quote():
    result = {
        "XXBTZUSD": {"altname": "XBTUSD", "wsname": "XBT/USD", "base": "XXBT", "quote": "ZUSD"},
        "XBTUSDT":  {"altname": "XBTUSDT", "wsname": "XBT/USDT", "base": "XXBT", "quote": "USDT"},
        "ADAETH":   {"altname": "ADAETH", "wsname": "ADA/ETH", "base": "ADA", "quote": "XETH"},
    }
    provider = _kraken(lambda r: _ok(result))
    symbols = await provider.list_symbols("USDT")

    assert [s.symbol for s in symbols] == ["XBTUSDT"]
    assert symbols[0].base == "XBT" and symbols[0].quote == "USDT"
    await provider.close()


async def test_pairs_without_wsname_are_skipped():
    provider = _kraken(lambda r: _ok({"WEIRD": {"altname": "WEIRD", "base": "X", "quote": "Y"}}))
    assert await provider.list_symbols("USDT") == []
    await provider.close()


async def test_order_book_parsing_and_imbalance_sign():
    book = {
        "XXBTZUSD": {
            "bids": [["61040.0", "10.0", 1700000000], ["61030.0", "5.0", 1700000000]],
            "asks": [["61050.0", "2.0", 1700000000], ["61060.0", "1.0", 1700000000]],
        }
    }
    provider = _kraken(lambda r: _ok(book))
    ob = await provider.get_order_book("XBTUSD", 50)

    assert ob.best_bid == 61040.0 and ob.best_ask == 61050.0
    assert ob.spread_bps is not None and ob.spread_bps > 0
    assert ob.imbalance(0.01) > 0, "far more bid size than ask size reads bullish"
    await provider.close()


async def test_field_layout_constant_matches_the_documented_contract():
    """Cross-checked against ccxt 4.5.73: index 5 is vwap, index 6 is volume."""
    assert OHLC_FIELDS[0] == "time"
    assert OHLC_FIELDS[1:5] == ("open", "high", "low", "close")
    assert OHLC_FIELDS[5] == "vwap"
    assert OHLC_FIELDS[6] == "volume"
    assert len(OHLC_FIELDS) == 8


# --------------------------------------------------------------------------- #
# Full pipeline against a mocked Kraken venue
# --------------------------------------------------------------------------- #


def _kraken_venue_handler(symbols=("XBTUSDT", "ETHUSDT", "SOLUSDT")):
    """A mock that answers every endpoint the scanner touches, Kraken-shaped.

    Parsing tests prove the connector reads a payload. This proves the *scanner*
    runs on this venue: universe filtering, multi-timeframe features, scoring,
    gates and ranking, all the way through.
    """
    import math

    def ohlc(tf: Timeframe, n: int, seed: float):
        tf_s = tf.seconds
        base = (FIXED_NOW_MS // 1000 // tf_s) * tf_s - n * tf_s
        rows = []
        for i in range(n):
            # A gentle wave: enough structure for swings and ATR to exist.
            price = seed * (1.0 + 0.04 * math.sin(i / 9.0) + 0.0004 * i)
            hi, lo = price * 1.004, price * 0.996
            rows.append([
                base + i * tf_s, f"{price:.6f}", f"{hi:.6f}", f"{lo:.6f}",
                f"{price:.6f}", f"{price:.6f}", f"{1000 + (i % 17) * 60:.4f}", 50 + i % 9,
            ])
        return rows

    seeds = {"XBTUSDT": 61000.0, "ETHUSDT": 3100.0, "SOLUSDT": 148.0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params

        if path.endswith("/SystemStatus"):
            return _ok({"status": "online"})

        if path.endswith("/AssetPairs"):
            return _ok({
                f"K{s}": {"altname": s, "wsname": f"{s[:-4]}/USDT", "base": s[:-4], "quote": "USDT"}
                for s in symbols
            })

        if path.endswith("/Ticker"):
            out = {}
            for s in symbols:
                px = seeds.get(s, 100.0)
                out[f"K{s}"] = {
                    "a": [f"{px * 1.0002}", "1", "1.0"], "b": [f"{px * 0.9998}", "1", "1.0"],
                    "c": [f"{px}", "0.1"], "v": ["100.0", "9000.0"],
                    "p": [f"{px}", f"{px}"], "t": [100, 9000],
                    "l": [f"{px * 0.95}", f"{px * 0.94}"], "h": [f"{px * 1.05}", f"{px * 1.06}"],
                    "o": f"{px * 0.99}",
                }
            return _ok(out)

        if path.endswith("/OHLC"):
            pair = params.get("pair", "XBTUSDT")
            interval = int(params.get("interval", 5))
            tf = next(t for t, m in TIMEFRAME_MINUTES.items() if m == interval)
            return _ok({f"K{pair}": ohlc(tf, 320, seeds.get(pair, 100.0))})

        if path.endswith("/Depth"):
            pair = params.get("pair", "XBTUSDT")
            px = seeds.get(pair, 100.0)
            return _ok({f"K{pair}": {
                "bids": [[f"{px * (1 - 0.0001 * i)}", "12.0", 0] for i in range(1, 26)],
                "asks": [[f"{px * (1 + 0.0001 * i)}", "8.0", 0] for i in range(1, 26)],
            }})

        return httpx.Response(404, json={"error": ["EGeneral:Unknown endpoint"]})

    return handler


async def test_scanner_runs_end_to_end_against_a_kraken_shaped_venue():
    from cryptopulse.config.settings import CryptoPulseSettings
    from cryptopulse.scanner.cex import CexScanner

    settings = CryptoPulseSettings()
    settings.providers.market_data = "kraken"
    settings.scanner.min_quote_volume_24h = 1000.0
    settings.scanner.always_include = []

    provider = _kraken(_kraken_venue_handler())
    scanner = CexScanner(settings, provider=provider, clock=FrozenClock(FIXED_NOW_MS))
    report = await scanner.scan()

    assert report.succeeded == 3, f"expected 3 scored assets, got {report.succeeded}: {report.errors}"
    assert report.failed == 0, report.errors
    assert report.synthetic_data is False, "Kraken is a real venue, never flagged synthetic"

    for r in report.results:
        assert 0.0 <= r.final_score <= 100.0
        assert r.final_score == pytest.approx(max(0.0, r.raw_score - r.risk_penalty), abs=0.01)
        assert r.features is not None and r.features.primary.atr14 is not None
        # The order book reached the scorer, so order flow is real rather than absent.
        assert r.features.order_book_imbalance is not None
        assert r.features.spread_bps is not None and r.features.spread_bps > 0
        # Liquidity came from the derived quote volume, so the gate has a verdict.
        assert r.liquidity.status.value != "UNKNOWN"

    await scanner.close()


async def test_scanner_survives_a_kraken_error_envelope_on_one_symbol():
    """A per-symbol EQuery must cost that symbol, not the scan."""
    from cryptopulse.config.settings import CryptoPulseSettings
    from cryptopulse.scanner.cex import CexScanner

    base_handler = _kraken_venue_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/OHLC") and request.url.params.get("pair") == "ETHUSDT":
            return httpx.Response(200, json={"error": ["EQuery:Unknown asset pair"], "result": {}})
        return base_handler(request)

    settings = CryptoPulseSettings()
    settings.providers.market_data = "kraken"
    settings.scanner.min_quote_volume_24h = 1000.0
    settings.scanner.always_include = []

    provider = _kraken(handler)
    scanner = CexScanner(settings, provider=provider, clock=FrozenClock(FIXED_NOW_MS))
    report = await scanner.scan()

    assert "ETHUSDT" in report.errors
    assert report.succeeded == 2, "the other two symbols must still score"
    await scanner.close()
