"""CEX scanner: DATA → FILTER → SCORE → VALIDATE → RANK.

Operational rules enforced here:

* One asset failing never stops the scan. Errors are collected per symbol and
  reported in `ScanReport.errors`, and the dashboard shows the failure count.
* The universe is pre-filtered on cheap data (24h tickers, one request for the
  whole venue) before any per-symbol kline request is spent. Scanning 120 symbols
  across 4 timeframes is ~480 kline calls; doing that for 2000 symbols would be
  both pointless and a fast route to a rate-limit ban.
* Order books are fetched only for the top candidates after a first-pass score,
  because depth is the most expensive data per unit of signal.
"""

from __future__ import annotations

import asyncio
import time

from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.errors import CryptoPulseError, SourceUnavailable
from cryptopulse.core.logging import get_logger
from cryptopulse.core.types import Timeframe
from cryptopulse.features.pipeline import AssetFeatures, TimeframeFeatures
from cryptopulse.features.regime import RegimeReport, classify_regime
from cryptopulse.providers.base import MarketDataProvider, OrderBookProvider
from cryptopulse.providers.registry import build_market_provider, is_synthetic
from cryptopulse.scanner.base import Scanner, ScanReport
from cryptopulse.scanner.memory import ScoreMemory, ScorePoint
from cryptopulse.scoring.engine import ScoreEngine, ScoreResult
from cryptopulse.scoring.explosion import ExplosionEngine

log = get_logger("scanner.cex")

__all__ = ["CexScanner"]

# How many top-ranked assets get an order book fetched each pass.
ORDER_BOOK_TOP_N = 20


class CexScanner(Scanner):
    name = "cex"

    def __init__(
        self,
        settings: CryptoPulseSettings,
        *,
        provider: MarketDataProvider | None = None,
        memory: ScoreMemory | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.settings = settings
        self.cfg = settings.scanner
        self.clock = clock
        self.provider = provider or build_market_provider(settings, clock)
        self.engine = ScoreEngine(settings)
        self.explosion_engine = ExplosionEngine()
        self.memory = memory or ScoreMemory()
        self.regime: RegimeReport = RegimeReport.unknown()
        self._last_report: ScanReport | None = None
        # The venue-wide ticker dictionary from the most recent scan. Kept so
        # the Token Hunter can read the whole venue without spending a single
        # extra request — that call has already been paid for.
        self.last_tickers: dict = {}

    async def close(self) -> None:
        await self.provider.close()

    @property
    def last_report(self) -> ScanReport | None:
        return self._last_report

    # ---------------------------------------------------------------- universe #

    async def _build_universe(self) -> tuple[list[str], dict, int]:
        """Cheap pre-filter. Returns (symbols, tickers, full universe size)."""
        tickers = await self.provider.get_tickers_24h()
        cfg = self.cfg
        quote = cfg.quote_asset.upper()

        candidates = []
        for symbol, t in tickers.items():
            if not symbol.endswith(quote):
                continue
            base = symbol[: -len(quote)]
            if base in cfg.exclude_stable_bases:
                continue
            if any(p in base for p in cfg.exclude_patterns):
                continue
            if t.quote_volume_24h < cfg.min_quote_volume_24h:
                continue
            candidates.append((symbol, t.quote_volume_24h))

        candidates.sort(key=lambda x: x[1], reverse=True)
        selected = [s for s, _ in candidates[: cfg.max_symbols]]

        # Reference majors are always scanned even if they fall out of the
        # volume ranking, because the dashboard is expected to show them.
        for sym in cfg.always_include:
            if sym in tickers and sym not in selected:
                selected.append(sym)

        return selected, tickers, len(tickers)

    # ---------------------------------------------------------------- features #

    async def _fetch_features(self, symbol: str, ticker) -> AssetFeatures:
        per_tf: dict[Timeframe, TimeframeFeatures] = {}
        structure_kwargs = {
            "breakout_confirm_atr": self.settings.scoring.breakout_confirm_atr,
            "retest_band_atr": self.settings.scoring.retest_band_atr,
        }

        series_list = await asyncio.gather(
            *(self.provider.get_ohlcv(symbol, tf, self.cfg.candles_per_timeframe) for tf in self.cfg.timeframes),
            return_exceptions=True,
        )

        warnings: list[str] = []
        for tf, series in zip(self.cfg.timeframes, series_list, strict=True):
            if isinstance(series, BaseException):
                warnings.append(f"{tf.value}: {type(series).__name__}")
                log.warning("timeframe_fetch_failed", symbol=symbol, timeframe=tf.value, error=str(series)[:160])
                continue
            try:
                per_tf[tf] = TimeframeFeatures.build(
                    series, min_bars=self.cfg.min_candles_required, structure_kwargs=structure_kwargs
                )
            except Exception as exc:
                warnings.append(f"{tf.value}: feature build failed")
                log.warning("feature_build_failed", symbol=symbol, timeframe=tf.value, error=str(exc)[:160])

        if self.cfg.primary_timeframe not in per_tf:
            raise SourceUnavailable(f"{symbol}: primary timeframe {self.cfg.primary_timeframe.value} unavailable")

        return AssetFeatures(
            symbol=symbol,
            primary_timeframe=self.cfg.primary_timeframe,
            timeframes=per_tf,
            quote_volume_24h=ticker.quote_volume_24h if ticker else None,
            price_change_pct_24h=ticker.price_change_pct_24h if ticker else None,
            ticker_provenance=ticker.provenance if ticker else None,
            warnings=warnings,
        )

    async def _attach_order_book(self, af: AssetFeatures) -> None:
        """Best-effort. A missing book lowers confidence; it never blocks a scan."""
        if not isinstance(self.provider, OrderBookProvider):
            return
        try:
            book = await self.provider.get_order_book(af.symbol, depth=100)
            af.order_book_imbalance = book.imbalance(0.005)
            af.spread_bps = book.spread_bps
            af.order_book_provenance = book.provenance
        except Exception as exc:
            log.info("order_book_unavailable", symbol=af.symbol, error=str(exc)[:120])

    # ---------------------------------------------------------------- regime #

    async def _update_regime(self) -> None:
        ref = "BTCUSDT"
        try:
            series = (await self.provider.get_ohlcv(ref, Timeframe.H4, 200)).closed()
            if len(series) >= 60:
                self.regime = classify_regime(series.high, series.low, series.close, reference_symbol=ref)
        except Exception as exc:
            log.info("regime_unavailable", error=str(exc)[:120])
            self.regime = RegimeReport.unknown(ref)

    # ---------------------------------------------------------------- main #

    async def scan(self) -> ScanReport:
        started = self.clock.now_ms()
        t0 = time.perf_counter()
        errors: dict[str, str] = {}
        notes: list[str] = []
        synthetic = is_synthetic(self.provider)
        if synthetic:
            notes.append(
                "SYNTHETIC DATA: the active provider generates candles; nothing in this report is market data."
            )

        health = await self.provider.health()
        if not health.available:
            log.error("provider_unavailable", provider=self.provider.name, detail=health.detail)
            finished = self.clock.now_ms()
            return ScanReport(
                started_at_ms=started,
                finished_at_ms=finished,
                scanned=0,
                succeeded=0,
                failed=0,
                errors={self.provider.name: f"SOURCE_UNAVAILABLE: {health.detail}"},
                provider_health=[health],
                synthetic_data=synthetic,
                notes=notes + ["scan aborted: data source unavailable"],
            )

        try:
            symbols, tickers, universe_size = await self._build_universe()
        except CryptoPulseError as exc:
            finished = self.clock.now_ms()
            return ScanReport(
                started_at_ms=started,
                finished_at_ms=finished,
                scanned=0,
                succeeded=0,
                failed=0,
                errors={"universe": exc.to_dict()["reason"] + ": " + exc.message},
                provider_health=[health],
                synthetic_data=synthetic,
                notes=notes + ["scan aborted: could not build universe"],
            )

        self.last_tickers = tickers

        await self._update_regime()

        # -- pass 1: features + first-pass score ------------------------------ #
        sem = asyncio.Semaphore(self.settings.providers.max_concurrent_requests)

        async def process(symbol: str) -> tuple[str, AssetFeatures | None, str | None]:
            async with sem:
                try:
                    af = await self._fetch_features(symbol, tickers.get(symbol))
                    return symbol, af, None
                except CryptoPulseError as exc:
                    return symbol, None, f"{exc.reason.value}: {exc.message}"
                except Exception as exc:  # one bad asset must not kill the scan
                    return symbol, None, f"{type(exc).__name__}: {exc}"

        gathered = await asyncio.gather(*(process(s) for s in symbols))

        features: list[AssetFeatures] = []
        for symbol, af, err in gathered:
            if err:
                errors[symbol] = err
            elif af is not None:
                features.append(af)

        now_ms = self.clock.now_ms()
        first_pass = [(af, self.engine.score(af, now_ms)) for af in features]
        first_pass.sort(key=lambda pair: pair[1].final_score, reverse=True)

        # -- pass 2: order books for the leaders, then rescore ---------------- #
        leaders = [af for af, _ in first_pass[:ORDER_BOOK_TOP_N]]
        await asyncio.gather(*(self._attach_order_book(af) for af in leaders), return_exceptions=True)

        results: list[ScoreResult] = []
        now_ms = self.clock.now_ms()
        for af, provisional in first_pass:
            result = self.engine.score(af, now_ms) if af in leaders else provisional

            # Scored here rather than inside ScoreEngine because it needs the
            # order book, which only exists after pass 2, and because it is a
            # separate engine answering a separate question. A hard gate zeroes
            # it outright: on a fifteen-minute horizon an illiquid token's ten
            # percent candle is a trap, and a merely-reduced score would still
            # let it outrank a tradeable one.
            result.explosion = self.explosion_engine.score(
                af,
                timestamp_ms=result.timestamp_ms,
                maturity_score=result.maturity.score,
                hard_veto=result.safety.hard_veto or result.liquidity.veto,
                veto_reason=(
                    "liquidity gate: a fast move here could not be exited"
                    if result.liquidity.veto
                    else "safety gate: this asset is flagged and a fast move is not an opportunity"
                    if result.safety.hard_veto
                    else None
                ),
            )

            self.memory.record(
                af.symbol,
                ScorePoint(
                    timestamp_ms=result.timestamp_ms,
                    final_score=result.final_score,
                    raw_score=result.raw_score,
                    price=result.price,
                    state=result.state.state.value,
                ),
            )
            delta, previous = self.memory.acceleration(af.symbol, now_ms)
            result.score_acceleration = delta
            result.previous_score = previous
            results.append(result)

        results.sort(key=self._rank_key, reverse=True)

        finished = self.clock.now_ms()
        report = ScanReport(
            started_at_ms=started,
            finished_at_ms=finished,
            scanned=len(symbols),
            succeeded=len(results),
            failed=len(errors),
            results=results,
            errors=errors,
            provider_health=[health],
            universe_size=universe_size,
            synthetic_data=synthetic,
            notes=notes,
        )
        self._last_report = report

        log.info(
            "scan_complete",
            provider=self.provider.name,
            universe=universe_size,
            scanned=len(symbols),
            succeeded=len(results),
            failed=len(errors),
            duration_ms=int((time.perf_counter() - t0) * 1000),
            regime=self.regime.trend.value,
            premium=sum(1 for r in results if r.is_premium),
            synthetic=synthetic,
        )
        return report

    @staticmethod
    def _rank_key(r: ScoreResult) -> float:
        """Rank by setup quality, not by price change.

        A rising score is worth real weight — that is the "changing behaviour"
        the product is built to find — and confidence scales the whole thing so a
        high score computed from thin data cannot outrank a solid one.
        """
        base = r.final_score
        accel_bonus = 0.0
        if r.score_acceleration is not None:
            accel_bonus = max(-5.0, min(12.0, r.score_acceleration * 0.6))
        confidence_factor = 0.7 + 0.3 * (r.confidence.score / 100.0)
        penalty = 15.0 if (r.safety.hard_veto or r.liquidity.veto) else 0.0
        return (base + accel_bonus) * confidence_factor - penalty
