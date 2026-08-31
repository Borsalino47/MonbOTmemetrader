"""CEX scanner: DATA → FILTER → ENRICH → SCORE → VALIDATE → RANK.

Operational rules enforced here:

* One asset failing never stops the scan. Errors are collected per symbol and
  reported in `ScanReport.errors`, and the dashboard shows the failure count.
* The universe is pre-filtered on cheap data (24h tickers, one request for the
  whole venue) before any per-symbol kline request is spent. Scanning 120 symbols
  across 4 timeframes is ~480 kline calls; doing that for 2000 symbols would be
  both pointless and a fast route to a rate-limit ban.
* Order books are fetched only for the top candidates after a first-pass score,
  because depth is the most expensive data per unit of signal.

THE ENRICH STEP, AND WHY IT HAS TO HAPPEN BEFORE SCORING

Three of the strongest readings this scanner has are not computable from one
asset in isolation, so they are attached to every `AssetFeatures` *before* the
first score rather than bolted on afterwards:

* **relative strength** — an asset rising while the benchmark falls is a
  different event from the same asset rising in a market where everything is up;
* **cross-sectional volume rank** — RVOL 2.5 means one thing on a dead Sunday
  and another when the whole board is at 2.5;
* **valuation** — market cap, which no candle contains and which decides whether
  a ×10 is arithmetically payable at all.

All three are `None` when they cannot be computed, and every consumer treats
`None` as unknown rather than as neutral.
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
from cryptopulse.features.stats import cross_sectional_percentile
from cryptopulse.providers.base import MarketDataProvider, OrderBookProvider
from cryptopulse.providers.registry import build_market_provider, build_valuation_provider, is_synthetic
from cryptopulse.scanner.base import Scanner, ScanReport
from cryptopulse.scanner.memory import ScoreMemory, ScorePoint
from cryptopulse.scoring.engine import ScoreEngine, ScoreResult
from cryptopulse.scoring.moonshot import MoonshotStage
from cryptopulse.universe.robinhood import UniverseResolution, load_bases, resolve_universe
from cryptopulse.universe.symbols import canonical_base, split_symbol

log = get_logger("scanner.cex")

__all__ = ["CexScanner"]

# How many top-ranked assets get an order book fetched each pass.
ORDER_BOOK_TOP_N = 20

# Bars used for relative strength. Matches `roc6`, which every timeframe already
# computes, so no extra series has to be kept.
RS_BARS = 6


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
        # Optional and allowed to be None: an absent market cap is reported as
        # unknown, never inferred.
        self.valuation_provider = build_valuation_provider(settings, clock)
        self.engine = ScoreEngine(settings)
        self.memory = memory or ScoreMemory()
        self.regime: RegimeReport = RegimeReport.unknown()
        self.universe_resolution: UniverseResolution | None = None
        self._benchmark_roc: dict[Timeframe, float] = {}
        self._last_report: ScanReport | None = None

    async def close(self) -> None:
        await self.provider.close()
        if self.valuation_provider is not None:
            await self.valuation_provider.close()

    @property
    def last_report(self) -> ScanReport | None:
        return self._last_report

    @property
    def benchmark_symbol(self) -> str:
        """What relative strength and the regime are measured against.

        Falls back to the provider's own reference symbol rather than a hardcoded
        BTCUSDT, which is simply wrong on a venue that calls bitcoin XBT.
        """
        return self.cfg.benchmark_symbol or getattr(self.provider, "reference_symbol", "BTCUSDT")

    @property
    def timeframes(self) -> list[Timeframe]:
        """Timeframes to fetch: the configured set, plus the moonshot timeframe.

        The daily is added rather than substituted — the intraday setup logic
        still needs its own timeframes, and a base is invisible on them.
        """
        tfs = list(self.cfg.timeframes)
        moon = self.settings.moonshot
        if moon.enabled and moon.timeframe not in tfs:
            tfs.append(moon.timeframe)
        return tfs

    # ---------------------------------------------------------------- universe #

    async def _build_universe(self) -> tuple[list[str], dict, int]:
        """Cheap pre-filter. Returns (symbols, tickers, full universe size)."""
        tickers = await self.provider.get_tickers_24h()
        if self.cfg.universe == "robinhood":
            return self._robinhood_universe(tickers)
        return self._volume_universe(tickers)

    def _robinhood_universe(self, tickers: dict) -> tuple[list[str], dict, int]:
        """Only what can be bought on Robinhood, resolved against this venue's names.

        Deliberately does NOT apply the volume floor: the whole point of the
        Robinhood universe is that it is small and fixed, and dropping a listed
        asset because it had a quiet day would hide exactly the dormant, based
        assets the ×10 layer exists to find. The liquidity gate still runs during
        scoring, so a genuinely untradable asset is vetoed rather than hidden.
        """
        bases, source, as_of = load_bases(
            file_path=self.cfg.robinhood_file,
            extra=self.cfg.robinhood_extra,
            exclude=self.cfg.robinhood_exclude,
        )
        resolution = resolve_universe(
            bases, list(tickers.keys()), self.cfg.quote_asset, source=source, as_of=as_of
        )
        self.universe_resolution = resolution
        if not resolution.symbols:
            log.error(
                "robinhood_universe_empty",
                venue=self.provider.name,
                quote=self.cfg.quote_asset,
                requested=len(bases),
            )
        return resolution.symbols[: self.cfg.max_symbols], tickers, len(tickers)

    def _volume_universe(self, tickers: dict) -> tuple[list[str], dict, int]:
        cfg = self.cfg
        quote = cfg.quote_asset.upper()
        self.universe_resolution = None

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

    def _candles_for(self, tf: Timeframe) -> int:
        """The moonshot timeframe needs a deeper window than the intraday ones.

        A 300-bar daily window is under a year, which is not enough to see the
        high a beaten-down asset is measured against.
        """
        moon = self.settings.moonshot
        if moon.enabled and tf is moon.timeframe:
            return max(self.cfg.candles_per_timeframe, moon.candles)
        return self.cfg.candles_per_timeframe

    async def _fetch_features(self, symbol: str, ticker) -> AssetFeatures:
        per_tf: dict[Timeframe, TimeframeFeatures] = {}
        structure_kwargs = {
            "breakout_confirm_atr": self.settings.scoring.breakout_confirm_atr,
            "retest_band_atr": self.settings.scoring.retest_band_atr,
        }

        timeframes = self.timeframes
        series_list = await asyncio.gather(
            *(self.provider.get_ohlcv(symbol, tf, self._candles_for(tf)) for tf in timeframes),
            return_exceptions=True,
        )

        warnings: list[str] = []
        for tf, series in zip(timeframes, series_list, strict=True):
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

    # ------------------------------------------------------- benchmark / regime #

    async def _update_benchmark(self) -> None:
        """Market regime plus the benchmark's own return on each relevant timeframe.

        One fetch per timeframe for the whole scan, not per asset: relative
        strength is the same subtraction for every symbol.
        """
        ref = self.benchmark_symbol
        self._benchmark_roc = {}
        wanted = {Timeframe.H4}
        if self.settings.moonshot.enabled:
            wanted.add(self.settings.moonshot.timeframe)

        regime_set = False
        for tf in wanted:
            try:
                series = (await self.provider.get_ohlcv(ref, tf, self._candles_for(tf))).closed()
            except Exception as exc:
                log.info("benchmark_unavailable", symbol=ref, timeframe=tf.value, error=str(exc)[:120])
                continue

            if tf is Timeframe.H4 and len(series) >= 60:
                self.regime = classify_regime(series.high, series.low, series.close, reference_symbol=ref)
                regime_set = True
            if len(series) > RS_BARS:
                past = float(series.close[-1 - RS_BARS])
                if past > 0:
                    self._benchmark_roc[tf] = (series.last_close - past) / past * 100.0

        if not regime_set:
            self.regime = RegimeReport.unknown(ref)

    # ---------------------------------------------------------------- enrich #

    def _apply_relative_strength(self, features: list[AssetFeatures]) -> None:
        """Asset return minus benchmark return, in percentage points, same window.

        Uses the slowest timeframe for which both sides are available, because
        outperforming bitcoin over six daily bars is a far stronger statement
        than doing so over six four-hour bars.
        """
        if not self._benchmark_roc:
            return
        order = sorted(self._benchmark_roc, key=lambda tf: tf.seconds, reverse=True)
        for af in features:
            for tf in order:
                own = af.timeframes.get(tf)
                if own is None or own.roc6 is None:
                    continue
                af.rs_vs_benchmark_pct = own.roc6 - self._benchmark_roc[tf]
                af.benchmark_symbol = f"{self.benchmark_symbol} ({tf.value}, {RS_BARS} bars)"
                break

    @staticmethod
    def _apply_cross_section(features: list[AssetFeatures]) -> None:
        """Rank each asset's RVOL against every other asset in this same scan."""
        population = [
            af.primary.rvol for af in features if af.primary.rvol is not None and af.primary.rvol == af.primary.rvol
        ]
        if len(population) < 5:  # a percentile over four assets is noise
            return
        for af in features:
            if af.primary.rvol is not None:
                af.rvol_percentile_universe = cross_sectional_percentile(af.primary.rvol, population)

    async def _attach_valuations(self, features: list[AssetFeatures]) -> str | None:
        """Market caps for the scanned assets. Returns a note when unavailable.

        One catalogue fetch covers the whole scan. Failure is not fatal and not
        silent: the moonshot layer reports capacity as unknown and says why.
        """
        if self.valuation_provider is None or not features:
            return None
        quote = self.cfg.quote_asset
        wanted: dict[str, list[AssetFeatures]] = {}
        for af in features:
            base = split_symbol(af.symbol, quote)
            if base:
                wanted.setdefault(canonical_base(base), []).append(af)
        if not wanted:
            return None

        try:
            valuations = await self.valuation_provider.get_valuations(sorted(wanted))
        except Exception as exc:
            log.warning("valuations_unavailable", error=str(exc)[:160])
            return f"valuation source unavailable ({type(exc).__name__}); market caps unknown this scan"

        for base, val in valuations.items():
            for af in wanted.get(base, []):
                af.valuation = val
        return None

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

        await self._update_benchmark()
        if self.universe_resolution is not None:
            notes.extend(self.universe_resolution.notes)

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

        # -- enrich: cross-asset context, before anything is scored ----------- #
        self._apply_cross_section(features)
        self._apply_relative_strength(features)
        valuation_note = await self._attach_valuations(features)
        if valuation_note:
            notes.append(valuation_note)

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
        if self.cfg.rank_mode != "setup":
            notes.append(f"ranked by {self.cfg.rank_mode} (CP_SCAN_RANK_MODE)")

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

    def _rank_key(self, r: ScoreResult) -> float:
        """Order the table according to what this deployment is hunting.

        `setup` is the V1 behaviour. `moonshot` puts the daily reading first and
        keeps setup quality only as a tie-break, which is what a ×10 radar wants:
        the best setup on a $40B asset is not the row you opened the app for.
        `blend` ranks on both.
        """
        setup = self._setup_rank(r)
        mode = self.cfg.rank_mode
        if mode == "setup":
            return setup

        moon = r.moonshot
        if moon is None or moon.stage is MoonshotStage.UNKNOWN:
            # Unknown must not outrank measured. It sorts below every asset that
            # produced a reading, rather than being treated as a zero score.
            return -1000.0 + setup * 0.01
        if mode == "moonshot":
            return moon.score * 10.0 + setup * 0.1
        return 0.5 * moon.score + 0.5 * setup

    @staticmethod
    def _setup_rank(r: ScoreResult) -> float:
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
