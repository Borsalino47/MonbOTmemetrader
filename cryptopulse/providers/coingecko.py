"""CoinGecko valuation connector — market cap, FDV and supply.

--------------------------------------------------------------------------
VERIFICATION STATUS: IMPLEMENTED, **NOT LIVE VERIFIED**. Written in an
environment with no egress to any market host. Run
`python -m cryptopulse.cli doctor --valuation coingecko` from a machine with
network access; it round-trips the endpoint and cross-checks the parsed fields
against each other.
--------------------------------------------------------------------------

WHY A SECOND SOURCE AT ALL

Market cap is the one input the ×10 layer cannot derive from candles, and it is
not a detail: ten times a $20M asset is an ordinary week somewhere in the
market, ten times a $40B asset would make it one of the largest that has ever
existed. Price says nothing about this — a $0.000001 token can be worth more
than a $60,000 one.

THE ONE DESIGN DECISION WORTH EXPLAINING

Tickers are not unique. Several assets trade as "SOL", and picking the wrong one
would hand the scanner a market cap off by three orders of magnitude — a silent,
confident error, the worst kind this project can produce. Two defences:

* the catalogue is read **ranked by market cap**, and when a ticker appears more
  than once the largest is used and the valuation is tagged
  `ambiguous_symbol=True`, which the moonshot layer surfaces as a caveat;
* an asset outside the ranked set does not become "unknown". It becomes a
  **bound**: its cap is smaller than the smallest cap in the ranking. That is a
  real fact, and a useful one — for a ×10 hunt, "smaller than the 500th largest
  asset" is closer to good news than to missing data. It is recorded in
  `market_cap_upper_bound_usd` and never promoted to `market_cap_usd`.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from cryptopulse.config.settings import ProviderSettings
from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.errors import SourceUnavailable
from cryptopulse.core.logging import get_logger
from cryptopulse.core.types import AssetValuation, DataQuality, Provenance
from cryptopulse.providers.base import ProviderHealth, ValuationProvider
from cryptopulse.providers.http import HttpClient

log = get_logger("providers.coingecko")

__all__ = ["CoinGeckoValuationProvider", "MARKETS_PATH", "parse_market_row"]

MARKETS_PATH = "/api/v3/coins/markets"
PER_PAGE = 250


def _f(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def parse_market_row(row: dict, provenance: Provenance) -> AssetValuation | None:
    """One row of /coins/markets. Returns None when the row carries no ticker."""
    symbol = str(row.get("symbol") or "").upper()
    if not symbol:
        return None
    rank = row.get("market_cap_rank")
    return AssetValuation(
        symbol=symbol,
        market_cap_usd=_f(row.get("market_cap")),
        fully_diluted_valuation_usd=_f(row.get("fully_diluted_valuation")),
        circulating_supply=_f(row.get("circulating_supply")),
        total_supply=_f(row.get("total_supply")) or _f(row.get("max_supply")),
        ath_usd=_f(row.get("ath")),
        ath_change_pct=_f(row.get("ath_change_percentage")),
        rank=int(rank) if isinstance(rank, (int, float)) else None,
        provenance=provenance,
    )


class CoinGeckoValuationProvider(ValuationProvider):
    name = "coingecko"

    def __init__(self, cfg: ProviderSettings, clock: Clock = SYSTEM_CLOCK) -> None:
        self.cfg = cfg
        self.clock = clock
        headers = {"x-cg-demo-api-key": cfg.coingecko_api_key} if cfg.coingecko_api_key else None
        self.http = HttpClient(
            cfg.coingecko_base_url,
            name=self.name,
            # CoinGecko's free tier is measured in calls per minute, not weight,
            # and is far tighter than an exchange's. Two pages an hour is well
            # inside it; this budget exists to make a bug harmless, not to be used.
            weight_per_minute=60,
            max_concurrent=2,
            timeout=cfg.request_timeout_seconds,
            max_retries=cfg.max_retries,
            retry_base_delay=cfg.retry_base_delay_seconds,
            circuit_failure_threshold=cfg.circuit_failure_threshold,
            circuit_reset_seconds=cfg.circuit_reset_seconds,
            headers=headers,
        )
        self._cache: dict[str, AssetValuation] = {}
        self._smallest_ranked_cap: float | None = None
        self._loaded_at: float = 0.0
        self._fetched_at_ms: int = 0

    async def close(self) -> None:
        await self.http.close()

    # -- health --------------------------------------------------------------- #

    async def health(self) -> ProviderHealth:
        started = time.perf_counter()
        try:
            await self._ensure_loaded()
            return ProviderHealth(
                name=self.name,
                available=bool(self._cache),
                latency_ms=(time.perf_counter() - started) * 1000,
                detail=f"{len(self._cache)} ranked assets cached",
                checked_at_ms=self.clock.now_ms(),
                rate_limit_remaining_pct=round(self.http.limiter.remaining_pct(), 1),
            )
        except Exception as exc:
            return ProviderHealth(
                name=self.name,
                available=False,
                detail=f"{type(exc).__name__}: {exc}",
                checked_at_ms=self.clock.now_ms(),
            )

    # -- catalogue ------------------------------------------------------------ #

    async def _ensure_loaded(self) -> None:
        """Refresh the ranked catalogue when the cache has aged out.

        Market caps move slowly relative to a scan interval, so re-fetching every
        minute would spend the rate-limit budget for no signal. A stale cache is
        visible: every valuation carries the fetch time in its provenance.
        """
        if self._cache and (time.monotonic() - self._loaded_at) < self.cfg.valuation_ttl_seconds:
            return

        fetched = self.clock.now_ms()
        provenance = Provenance(source=self.name, as_of_ms=fetched, fetched_at_ms=fetched)
        cache: dict[str, AssetValuation] = {}
        seen_twice: set[str] = set()
        smallest: float | None = None

        for page in range(1, max(1, self.cfg.valuation_pages) + 1):
            payload = await self.http.get_json(
                MARKETS_PATH,
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": PER_PAGE,
                    "page": page,
                    "sparkline": "false",
                },
                weight=1,
            )
            if not isinstance(payload, list):
                raise SourceUnavailable(f"coingecko: {MARKETS_PATH} returned {type(payload).__name__}, expected a list")
            if not payload:
                break

            for row in payload:
                if not isinstance(row, dict):
                    continue
                val = parse_market_row(row, provenance)
                if val is None:
                    continue
                existing = cache.get(val.symbol)
                if existing is not None:
                    # Ranked by cap descending, so the first one seen is the
                    # largest. Keep it, and remember that the ticker is not unique.
                    seen_twice.add(val.symbol)
                    continue
                cache[val.symbol] = val
                if val.market_cap_usd and (smallest is None or val.market_cap_usd < smallest):
                    smallest = val.market_cap_usd

        if not cache:
            raise SourceUnavailable("coingecko: catalogue contained no parsable rows")

        for symbol in seen_twice:
            cache[symbol].ambiguous_symbol = True

        self._cache = cache
        self._smallest_ranked_cap = smallest
        self._loaded_at = time.monotonic()
        self._fetched_at_ms = fetched
        log.info(
            "valuations_loaded",
            assets=len(cache),
            ambiguous=len(seen_twice),
            smallest_ranked_cap=smallest,
        )

    # -- reads ---------------------------------------------------------------- #

    async def get_valuations(self, base_assets: Sequence[str]) -> dict[str, AssetValuation]:
        await self._ensure_loaded()
        out: dict[str, AssetValuation] = {}
        bound_provenance = Provenance(
            source=self.name,
            as_of_ms=self._fetched_at_ms,
            fetched_at_ms=self._fetched_at_ms,
            quality=DataQuality.PARTIAL,
            note="market cap not ranked; value is an upper bound, not a measurement",
        )
        for base in base_assets:
            key = base.upper()
            hit = self._cache.get(key)
            if hit is not None:
                out[key] = hit
            elif self._smallest_ranked_cap is not None:
                out[key] = AssetValuation(
                    symbol=key,
                    market_cap_usd=None,
                    market_cap_upper_bound_usd=self._smallest_ranked_cap,
                    provenance=bound_provenance,
                )
        return out
