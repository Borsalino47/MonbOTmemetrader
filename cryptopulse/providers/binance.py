"""Binance Spot public REST connector.

--------------------------------------------------------------------------
VERIFICATION STATUS: IMPLEMENTED, **NOT LIVE VERIFIED**.

This connector was written in a sandbox whose egress policy blocks
`api.binance.com`, `data-api.binance.vision` and `developers.binance.com`
(the gateway answers 403 to CONNECT). It has therefore never exchanged a
packet with Binance, and the official documentation could not be re-read to
confirm the current field order of the kline payload or the current request
weights.

The endpoint shapes below have been **cross-checked against the python-binance
reference implementation** (v1.0.37, read offline from PyPI, which this sandbox
can reach even though Binance itself is blocked). That check confirmed:

  * the 12-column kline layout encoded in KLINE_FIELDS, index for index;
  * the klines limit of 1000 and the depth default of 100;
  * base URL https://api.binance.com/api + public API version v3;
  * the paths ping / exchangeInfo / ticker/24hr / klines / depth;
  * the ticker field names lastPrice, priceChangePercent, highPrice, lowPrice,
    closeTime, count, volume, weightedAvgPrice, quoteVolume;
  * the exchangeInfo symbol fields and the PRICE_FILTER / LOT_SIZE /
    MIN_NOTIONAL filter names.

That raises confidence but is still **not** verification: an independent library
agreeing with me proves we share the same understanding, not that the live API
matches it today. Before trusting this in production run:

    python -m cryptopulse.cli doctor

which performs a real round-trip against every endpoint used here, checks the
parsed values against independent fields in the same response, and prints
LIVE VERIFIED or the exact mismatch. Until that command has been run in an
environment with egress, treat this module as unverified.
--------------------------------------------------------------------------

Endpoints used (all public, no API key required):

  GET /api/v3/ping           weight 1    liveness
  GET /api/v3/exchangeInfo   weight ~20  tradable symbols and filters
  GET /api/v3/ticker/24hr    weight 2/symbol, ~80 for the full list
  GET /api/v3/klines         weight 2    OHLCV, max 1000 rows
  GET /api/v3/depth          weight 1-50 by limit; we use limit<=100 (weight 5)

Only public market-data endpoints are called. The connector never signs a
request and never sends an API key, because V1 places no orders (see PAPER_MODE).
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
from cryptopulse.providers.base import MarketDataProvider, OrderBookProvider, ProviderHealth
from cryptopulse.providers.http import HttpClient

log = get_logger("providers.binance")

__all__ = ["BinanceSpotProvider", "KLINE_FIELDS"]

# Documented kline row layout. Indices are asserted against this in `doctor`.
KLINE_FIELDS = (
    "open_time",       # 0  int   ms
    "open",            # 1  str
    "high",            # 2  str
    "low",             # 3  str
    "close",           # 4  str
    "volume",          # 5  str   base asset volume
    "close_time",      # 6  int   ms, inclusive end (open_time + interval - 1ms)
    "quote_volume",    # 7  str   quote asset volume
    "trades",          # 8  int
    "taker_buy_base",  # 9  str
    "taker_buy_quote", # 10 str
    "ignore",          # 11
)


class BinanceSpotProvider(MarketDataProvider, OrderBookProvider):
    name = "binance-spot"
    reference_symbol = "BTCUSDT"

    def __init__(self, cfg: ProviderSettings, clock: Clock = SYSTEM_CLOCK) -> None:
        self.cfg = cfg
        self.clock = clock
        self.http = HttpClient(
            cfg.binance_base_url,
            name=self.name,
            weight_per_minute=cfg.request_weight_per_minute,
            max_concurrent=cfg.max_concurrent_requests,
            timeout=cfg.request_timeout_seconds,
            max_retries=cfg.max_retries,
            retry_base_delay=cfg.retry_base_delay_seconds,
            circuit_failure_threshold=cfg.circuit_failure_threshold,
            circuit_reset_seconds=cfg.circuit_reset_seconds,
            fallback_base_url=cfg.binance_fallback_base_url,
        )
        self._symbol_cache: list[SymbolInfo] | None = None
        self._symbol_cache_at: float = 0.0

    async def close(self) -> None:
        await self.http.close()

    # -- health -------------------------------------------------------------- #

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            await self.http.get_json("/api/v3/ping", weight=1)
            return ProviderHealth(
                name=self.name,
                available=True,
                latency_ms=(time.perf_counter() - started) * 1000,
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

    async def list_symbols(self, quote_asset: str) -> list[SymbolInfo]:
        # exchangeInfo is expensive and near-static; cache for an hour.
        if self._symbol_cache is not None and (time.monotonic() - self._symbol_cache_at) < 3600:
            return [s for s in self._symbol_cache if s.quote == quote_asset.upper()]

        payload = await self.http.get_json("/api/v3/exchangeInfo", weight=20)
        if not isinstance(payload, dict) or "symbols" not in payload:
            raise SourceUnavailable("binance: exchangeInfo returned an unexpected shape")

        out: list[SymbolInfo] = []
        for row in payload["symbols"]:
            try:
                info = SymbolInfo(
                    symbol=row["symbol"],
                    base=row["baseAsset"],
                    quote=row["quoteAsset"],
                    status=row.get("status", "UNKNOWN"),
                )
                for filt in row.get("filters", []):
                    ftype = filt.get("filterType")
                    if ftype == "PRICE_FILTER":
                        info.tick_size = float(filt["tickSize"])
                    elif ftype == "LOT_SIZE":
                        info.step_size = float(filt["stepSize"])
                    elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
                        val = filt.get("minNotional")
                        if val is not None:
                            info.min_notional = float(val)
                out.append(info)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("symbol_parse_failed", row=str(row)[:120], error=str(exc))
                continue

        self._symbol_cache = out
        self._symbol_cache_at = time.monotonic()
        return [s for s in out if s.quote == quote_asset.upper()]

    # -- tickers ------------------------------------------------------------- #

    async def get_tickers_24h(self, symbols: Sequence[str] | None = None) -> dict[str, Ticker24h]:
        params: dict | None = None
        weight = 80
        if symbols:
            # The array form is documented as ["A","B"] with no spaces.
            params = {"symbols": "[" + ",".join(f'"{s}"' for s in symbols) + "]"}
            weight = min(80, max(2, 2 * len(symbols)))

        payload = await self.http.get_json("/api/v3/ticker/24hr", params=params, weight=weight)
        rows = payload if isinstance(payload, list) else [payload]
        fetched = self.clock.now_ms()
        out: dict[str, Ticker24h] = {}

        for row in rows:
            try:
                as_of = int(row.get("closeTime", fetched))
                quote_volume, qv_note = _quote_volume_from(row)
                out[row["symbol"]] = Ticker24h(
                    symbol=row["symbol"],
                    last_price=float(row["lastPrice"]),
                    quote_volume_24h=quote_volume,
                    price_change_pct_24h=float(row["priceChangePercent"]),
                    high_24h=float(row["highPrice"]),
                    low_24h=float(row["lowPrice"]),
                    trades_24h=int(row.get("count", 0)),
                    provenance=Provenance(
                        source=self.name, as_of_ms=as_of, fetched_at_ms=fetched, note=qv_note
                    ),
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("ticker_parse_failed", row=str(row)[:120], error=str(exc))
                continue

        if not out:
            raise DataUnavailable("binance: no parsable 24h tickers in response")
        return out

    # -- klines -------------------------------------------------------------- #

    async def get_ohlcv(self, symbol: str, timeframe: Timeframe, limit: int) -> OHLCVSeries:
        limit = max(1, min(int(limit), 1000))  # documented maximum is 1000
        payload = await self.http.get_json(
            "/api/v3/klines",
            params={"symbol": symbol.upper(), "interval": timeframe.value, "limit": limit},
            weight=2,
        )
        if not isinstance(payload, list):
            raise SourceUnavailable(f"binance: klines returned {type(payload).__name__}, expected list")
        if not payload:
            raise DataUnavailable(f"binance: no klines for {symbol} {timeframe.value}")

        fetched = self.clock.now_ms()
        now_ms = fetched
        candles: list[Candle] = []
        for row in payload:
            try:
                close_time = int(row[6])
                candles.append(
                    Candle(
                        open_time_ms=int(row[0]),
                        open=float(row[1]),
                        high=float(row[2]),
                        low=float(row[3]),
                        close=float(row[4]),
                        volume=float(row[5]),
                        close_time_ms=close_time,
                        quote_volume=float(row[7]),
                        trades=int(row[8]),
                        # Binance returns the in-progress candle as the last row.
                        # A candle is finished only once its close_time has passed.
                        is_closed=close_time <= now_ms,
                    )
                )
            except (IndexError, TypeError, ValueError) as exc:
                log.warning("kline_parse_failed", symbol=symbol, row=str(row)[:120], error=str(exc))
                continue

        if not candles:
            raise DataUnavailable(f"binance: all kline rows unparsable for {symbol} {timeframe.value}")

        newest_closed = max((c.close_time_ms for c in candles if c.is_closed), default=candles[-1].close_time_ms)
        series = OHLCVSeries.from_candles(
            symbol=symbol.upper(),
            timeframe=timeframe,
            candles=candles,
            provenance=Provenance(source=self.name, as_of_ms=newest_closed, fetched_at_ms=fetched),
        )

        # Tag discontinuities rather than papering over them; safety scoring reads this.
        if series.has_gaps():
            series.provenance = Provenance(
                source=self.name,
                as_of_ms=newest_closed,
                fetched_at_ms=fetched,
                quality=DataQuality.PARTIAL,
                note="GAPS",
            )
        return series

    # -- order book ---------------------------------------------------------- #

    async def get_order_book(self, symbol: str, depth: int = 100) -> OrderBook:
        depth = min(max(depth, 5), 100)  # keep the request in the weight-5 tier
        payload = await self.http.get_json(
            "/api/v3/depth", params={"symbol": symbol.upper(), "limit": depth}, weight=5
        )
        if not isinstance(payload, dict) or "bids" not in payload or "asks" not in payload:
            raise SourceUnavailable("binance: depth returned an unexpected shape")

        fetched = self.clock.now_ms()

        def levels(rows) -> list[OrderBookLevel]:
            out = []
            for r in rows:
                try:
                    out.append(OrderBookLevel(price=float(r[0]), qty=float(r[1])))
                except (IndexError, TypeError, ValueError):
                    continue
            return out

        return OrderBook(
            symbol=symbol.upper(),
            bids=levels(payload["bids"]),
            asks=levels(payload["asks"]),
            # REST depth carries no exchange timestamp, so the fetch time is the
            # best honest estimate of the observation time. Recorded as such.
            provenance=Provenance(
                source=self.name, as_of_ms=fetched, fetched_at_ms=fetched, note="REST snapshot, no venue timestamp"
            ),
        )


def _quote_volume_from(row: dict) -> tuple[float, str | None]:
    """24h volume in the quote asset, with a disclosed fallback.

    `quoteVolume` is the field the liquidity gate runs on, and the gate is the
    thing standing between the user and an asset they cannot exit. A missing key
    would drop the whole symbol from the universe via the parse-error path, which
    is a silent, hard-to-notice failure for the most important input in the system.

    `weightedAvgPrice * volume` is not an estimate — it is the definition of quote
    volume, and Binance documents `weightedAvgPrice` as `quoteVolume / volume`.
    Using it is an identity, not a guess. It is still tagged in the provenance so
    the substitution is visible rather than assumed.

    If neither is available the caller gets a KeyError and the row is skipped:
    better a missing asset than a liquidity verdict built on nothing.
    """
    raw = row.get("quoteVolume")
    if raw is not None:
        value = float(raw)
        if value > 0:
            return value, None

    weighted = row.get("weightedAvgPrice")
    volume = row.get("volume")
    if weighted is not None and volume is not None:
        derived = float(weighted) * float(volume)
        if derived > 0:
            return derived, "quote volume derived from weightedAvgPrice * volume"

    if raw is not None:
        return float(raw), None  # genuinely zero volume; report it as such
    raise KeyError("quoteVolume")
