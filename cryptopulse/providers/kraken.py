"""Kraken public REST connector — the alternative venue.

--------------------------------------------------------------------------
VERIFICATION STATUS: IMPLEMENTED, **NOT LIVE VERIFIED**.

Written in an environment whose egress allowlist refuses every exchange host,
so it has never exchanged a packet with Kraken. The contract below was
cross-checked against ccxt 4.5.73's `kraken.py`, read offline from PyPI —
a second opinion, not a verification.

Run `python -m cryptopulse.cli doctor --provider kraken` from a machine with
network access. It prints LIVE VERIFIED or the exact mismatch.
--------------------------------------------------------------------------

Why a second venue exists at all: Binance geo-blocks several jurisdictions
outright (`api.binance.com` returns 451 in the US), so a scanner with one
hardcoded exchange is unusable for a large share of users. Kraken is reachable
where Binance is not, needs no API key for market data, and is a genuinely
independent quote source rather than a mirror.

Endpoints used (all public, unauthenticated), base `https://api.kraken.com`:

  GET /0/public/SystemStatus   liveness
  GET /0/public/AssetPairs     tradable pairs
  GET /0/public/Ticker         24h statistics
  GET /0/public/OHLC           candles, max 720 rows, interval in MINUTES
  GET /0/public/Depth          order book

THREE DIFFERENCES FROM BINANCE that this module absorbs so nothing above the
provider layer has to know about them:

1. **Errors arrive with HTTP 200.** Every response is
   `{"error": [...], "result": {...}}`, and a failed call still returns 200 with
   a populated `error` array. Checking the status code alone would read a hard
   failure as success — the single most dangerous way this connector could lie.

2. **The pair you ask for is not the key you get back.** Request `XBTUSD` and the
   result is keyed `XXBTZUSD` (Kraken's legacy X/Z asset prefixes). This module
   never assumes the response key matches the request; it takes whatever key is
   present besides the `last` cursor.

3. **Timestamps are in seconds and intervals are integer minutes**, against
   Binance's milliseconds and interval strings.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from cryptopulse.config.settings import ProviderSettings
from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.errors import DataUnavailable, SourceUnavailable
from cryptopulse.core.logging import get_logger
from cryptopulse.core.types import (
    Candle,
    DataQuality,
    OHLCVSeries,
    OrderBook,
    OrderBookLevel,
    Provenance,
    SymbolInfo,
    Ticker24h,
    Timeframe,
)
from cryptopulse.providers.base import (
    MarketDataProvider,
    OrderBookProvider,
    ProviderHealth,
    TokenDiscoveryProvider,
)
from cryptopulse.providers.http import HttpClient

log = get_logger("providers.kraken")

__all__ = ["KrakenProvider", "OHLC_FIELDS", "TIMEFRAME_MINUTES"]

# Documented OHLC row layout. Note index 5 is VWAP and index 6 is the volume —
# getting these the wrong way round would silently feed VWAP into every volume
# feature, which is exactly the class of bug `doctor` exists to catch.
OHLC_FIELDS = (
    "time",    # 0  int   SECONDS
    "open",    # 1  str
    "high",    # 2  str
    "low",     # 3  str
    "close",   # 4  str
    "vwap",    # 5  str
    "volume",  # 6  str   base asset volume
    "count",   # 7  int   trade count
)

# Kraken takes the candle interval as an integer number of minutes.
TIMEFRAME_MINUTES: dict[Timeframe, int] = {
    Timeframe.M1: 1,
    Timeframe.M5: 5,
    Timeframe.M15: 15,
    Timeframe.H1: 60,
    Timeframe.H4: 240,
    Timeframe.D1: 1440,
}

MAX_OHLC_ROWS = 720


class KrakenProvider(MarketDataProvider, OrderBookProvider, TokenDiscoveryProvider):
    name = "kraken"
    reference_symbol = "XBTUSDT"

    def __init__(self, cfg: ProviderSettings, clock: Clock = SYSTEM_CLOCK) -> None:
        self.cfg = cfg
        self.clock = clock
        self.http = HttpClient(
            cfg.kraken_base_url,
            name=self.name,
            weight_per_minute=cfg.request_weight_per_minute,
            max_concurrent=cfg.max_concurrent_requests,
            timeout=cfg.request_timeout_seconds,
            max_retries=cfg.max_retries,
            retry_base_delay=cfg.retry_base_delay_seconds,
            circuit_failure_threshold=cfg.circuit_failure_threshold,
            circuit_reset_seconds=cfg.circuit_reset_seconds,
        )
        self._pairs: dict[str, str] | None = None  # altname -> Kraken pair id
        self._pairs_at: float = 0.0

    async def close(self) -> None:
        await self.http.close()

    # -- envelope ------------------------------------------------------------ #

    async def _get(self, path: str, params: dict | None = None, *, weight: int = 1) -> dict:
        """Call an endpoint and unwrap Kraken's `{error, result}` envelope.

        A populated `error` array arrives with HTTP 200, so the transport layer
        sees a success. Treating that as data is how a connector ends up
        reporting confident nonsense; it is raised here instead.
        """
        payload = await self.http.get_json(path, params=params, weight=weight)
        if not isinstance(payload, dict):
            raise SourceUnavailable(f"kraken: {path} returned {type(payload).__name__}, expected an object")

        errors = payload.get("error") or []
        if errors:
            joined = "; ".join(str(e) for e in errors)
            # Kraken namespaces its errors: EGeneral:, EQuery:, EService: ...
            if any(str(e).startswith("EService") for e in errors):
                raise SourceUnavailable(f"kraken: service unavailable: {joined}")
            raise DataUnavailable(f"kraken: {path}: {joined}")

        result = payload.get("result")
        if result is None:
            raise SourceUnavailable(f"kraken: {path} returned no result block")
        return result

    @staticmethod
    def _first_pair_key(result: dict) -> str | None:
        """The pair key Kraken chose, which is not the one we asked for.

        `XBTUSD` comes back as `XXBTZUSD`. Rather than replicate Kraken's legacy
        asset-prefix rules, take whatever key is there — `last` is the pagination
        cursor on OHLC responses, never a pair.
        """
        for key in result:
            if key != "last":
                return key
        return None

    # -- health -------------------------------------------------------------- #

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            result = await self._get("/0/public/SystemStatus", weight=1)
            status = str(result.get("status", "unknown"))
            # Kraken reports degraded modes explicitly: online / maintenance /
            # cancel_only / post_only. Only "online" is a healthy feed.
            return ProviderHealth(
                name=self.name,
                available=status == "online",
                latency_ms=(time.perf_counter() - started) * 1000,
                detail=None if status == "online" else f"exchange status: {status}",
                checked_at_ms=self.clock.now_ms(),
                rate_limit_remaining_pct=round(self.http.limiter.remaining_pct(), 1),
            )
        except Exception as exc:
            return ProviderHealth(
                name=self.name,
                available=False,
                detail=f"{type(exc).__name__}: {exc}",
                checked_at_ms=self.clock.now_ms(),
                rate_limit_remaining_pct=round(self.http.limiter.remaining_pct(), 1),
            )

    # -- universe ------------------------------------------------------------ #

    async def _ensure_pairs(self) -> dict[str, str]:
        """Build (and cache) the altname -> Kraken pair-id map.

        Every response is keyed by Kraken's internal pair id (`XXBTZUSD`), not by
        the tradable name (`XBTUSD`), so without this map the scanner would
        surface legacy ids as symbol names and its quote-asset filter — which
        matches on the symbol suffix — would reject perfectly good pairs.

        The scanner reaches `get_tickers_24h` without ever calling
        `list_symbols`, so the map has to build itself on first use rather than
        depending on call order.
        """
        if self._pairs is not None and (time.monotonic() - self._pairs_at) < 3600:
            return self._pairs
        await self.list_symbols("")  # populates the cache for every quote asset
        return self._pairs or {}

    async def list_symbols(self, quote_asset: str) -> list[SymbolInfo]:
        result = await self._get("/0/public/AssetPairs", weight=10)
        quote = quote_asset.upper()

        pairs: dict[str, str] = {}
        out: list[SymbolInfo] = []
        for pair_id, row in result.items():
            try:
                altname = row["altname"]
                # `wsname` is the only field with an unambiguous BASE/QUOTE split;
                # `base`/`quote` still carry Kraken's legacy X/Z prefixes.
                wsname = row.get("wsname")
                if not wsname or "/" not in wsname:
                    continue
                base_sym, quote_sym = wsname.split("/", 1)
                # The map covers every pair regardless of quote asset; the filter
                # below only narrows what is *returned*.
                pairs[altname] = pair_id
                if quote and quote_sym.upper() != quote:
                    continue
                out.append(
                    SymbolInfo(
                        symbol=altname,
                        base=base_sym,
                        quote=quote_sym,
                        status=row.get("status", "online"),
                        tick_size=None,
                        step_size=None,
                        min_notional=_float_or_none(row.get("ordermin")),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("pair_parse_failed", pair=str(pair_id)[:40], error=str(exc))
                continue

        self._pairs = pairs
        self._pairs_at = time.monotonic()
        return out

    # -- tickers ------------------------------------------------------------- #

    async def get_tickers_24h(self, symbols: Sequence[str] | None = None) -> dict[str, Ticker24h]:
        pairs = await self._ensure_pairs()
        params = {"pair": ",".join(symbols)} if symbols else None
        result = await self._get("/0/public/Ticker", params=params, weight=10)
        fetched = self.clock.now_ms()

        # Map Kraken's response keys back to the tradable names the scanner uses.
        reverse = {pair_id: alt for alt, pair_id in pairs.items()}

        out: dict[str, Ticker24h] = {}
        for pair_id, row in result.items():
            try:
                symbol = reverse.get(pair_id, pair_id)
                # Index 1 of v/p/t/l/h is the rolling 24h figure; index 0 is today.
                base_volume = float(row["v"][1])
                vwap = float(row["p"][1])
                last = float(row["c"][0])
                open_price = float(row["o"]) if not isinstance(row["o"], list) else float(row["o"][0])

                # Kraken publishes no quote volume, but VWAP is defined as
                # quote/base over the same window, so base * vwap is the quote
                # volume by identity — not an estimate. Tagged all the same.
                quote_volume = base_volume * vwap
                change_pct = ((last - open_price) / open_price * 100.0) if open_price else 0.0

                out[symbol] = Ticker24h(
                    symbol=symbol,
                    last_price=last,
                    quote_volume_24h=quote_volume,
                    price_change_pct_24h=change_pct,
                    high_24h=float(row["h"][1]),
                    low_24h=float(row["l"][1]),
                    trades_24h=int(row["t"][1]),
                    provenance=Provenance(
                        source=self.name,
                        # Kraken's ticker carries no timestamp; the fetch time is
                        # the honest observation time, and it is labelled as such.
                        as_of_ms=fetched,
                        fetched_at_ms=fetched,
                        note="quote volume derived from base volume * VWAP; no venue timestamp",
                    ),
                )
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                log.warning("ticker_parse_failed", pair=str(pair_id)[:40], error=str(exc))
                continue

        if not out:
            raise DataUnavailable("kraken: no parsable tickers in response")
        return out

    # -- token discovery ------------------------------------------------------ #

    async def discover_universe(self, quote_asset: str) -> dict[str, Ticker24h]:
        """The whole venue in one request — the call that makes a wide search viable."""
        return await self.get_tickers_24h(None)

    # -- klines -------------------------------------------------------------- #

    async def get_ohlcv(self, symbol: str, timeframe: Timeframe, limit: int) -> OHLCVSeries:
        interval = TIMEFRAME_MINUTES.get(timeframe)
        if interval is None:
            raise DataUnavailable(f"kraken: timeframe {timeframe.value} is not offered")

        limit = max(1, min(int(limit), MAX_OHLC_ROWS))
        tf_ms = timeframe.ms
        now_ms = self.clock.now_ms()
        # Kraken has no `limit` parameter — it returns everything after `since`,
        # capped at 720 rows. Asking from the right point is how we bound it.
        since_s = (now_ms - (limit + 1) * tf_ms) // 1000

        result = await self._get(
            "/0/public/OHLC",
            params={"pair": symbol.upper(), "interval": interval, "since": since_s},
            weight=2,
        )
        # Kraken accepts either the tradable name or the legacy id on input; the
        # response key is always the legacy id, which `_first_pair_key` absorbs.
        key = self._first_pair_key(result)
        if key is None:
            raise DataUnavailable(f"kraken: no candle series for {symbol}")

        rows = result[key]
        if not isinstance(rows, list) or not rows:
            raise DataUnavailable(f"kraken: empty candle series for {symbol} {timeframe.value}")

        candles: list[Candle] = []
        for row in rows:
            try:
                open_time = int(row[0]) * 1000  # seconds -> ms
                close_time = open_time + tf_ms - 1
                volume = float(row[6])
                close = float(row[4])
                candles.append(
                    Candle(
                        open_time_ms=open_time,
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=close,
                        volume=volume,
                        close_time_ms=close_time,
                        # Kraken gives no quote volume per candle; VWAP * volume
                        # is the identity, and falls back to close * volume when
                        # VWAP is zero (a candle with no trades).
                        quote_volume=(float(row[5]) or close) * volume,
                        trades=int(row[7]),
                        is_closed=close_time <= now_ms,
                    )
                )
            except (IndexError, TypeError, ValueError) as exc:
                log.warning("ohlc_parse_failed", symbol=symbol, row=str(row)[:120], error=str(exc))
                continue

        if not candles:
            raise DataUnavailable(f"kraken: all candle rows unparsable for {symbol} {timeframe.value}")

        candles = candles[-limit:]
        newest_closed = max((c.close_time_ms for c in candles if c.is_closed), default=candles[-1].close_time_ms)
        series = OHLCVSeries.from_candles(
            symbol=symbol.upper(),
            timeframe=timeframe,
            candles=candles,
            provenance=Provenance(source=self.name, as_of_ms=newest_closed, fetched_at_ms=now_ms),
        )
        if series.has_gaps():
            series.provenance = Provenance(
                source=self.name,
                as_of_ms=newest_closed,
                fetched_at_ms=now_ms,
                quality=DataQuality.PARTIAL,
                note="GAPS",
            )
        return series

    # -- order book ---------------------------------------------------------- #

    async def get_order_book(self, symbol: str, depth: int = 100) -> OrderBook:
        depth = min(max(depth, 5), 500)
        result = await self._get(
            "/0/public/Depth", params={"pair": symbol.upper(), "count": depth}, weight=2
        )
        key = self._first_pair_key(result)
        if key is None:
            raise DataUnavailable(f"kraken: no order book for {symbol}")

        book = result[key]
        fetched = self.clock.now_ms()

        def levels(rows) -> list[OrderBookLevel]:
            out = []
            for r in rows or []:
                try:
                    # Kraken rows are [price, volume, timestamp]; the third field
                    # is per-level and not the book's own age.
                    out.append(OrderBookLevel(price=float(r[0]), qty=float(r[1])))
                except (IndexError, TypeError, ValueError):
                    continue
            return out

        return OrderBook(
            symbol=symbol.upper(),
            bids=levels(book.get("bids")),
            asks=levels(book.get("asks")),
            provenance=Provenance(
                source=self.name,
                as_of_ms=fetched,
                fetched_at_ms=fetched,
                note="REST snapshot, no venue timestamp",
            ),
        )


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
