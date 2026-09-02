"""CoinGecko valuation connector: parsing, ticker collisions, and the cap bound.

The dangerous failure here is not an outage — an outage leaves capacity unknown
and the moonshot layer says so. It is a *confident* wrong answer: the wrong
asset's market cap silently attached to a ticker. That is what the ambiguity and
bound tests below exist for.

Parsed against mocked payloads shaped like CoinGecko's /coins/markets. The live
round-trip lives in `cryptopulse.cli doctor --valuation coingecko`.
"""

from __future__ import annotations

import httpx
import pytest

from cryptopulse.config.settings import ProviderSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.core.errors import SourceUnavailable
from cryptopulse.providers.coingecko import MARKETS_PATH, CoinGeckoValuationProvider
from tests.conftest import FIXED_NOW_MS

pytestmark = pytest.mark.asyncio


def _row(symbol: str, cap: float, rank: int, **extra) -> dict:
    return {
        "id": symbol.lower(),
        "symbol": symbol.lower(),
        "name": symbol,
        "market_cap": cap,
        "market_cap_rank": rank,
        "fully_diluted_valuation": cap * 2,
        "circulating_supply": 1_000_000,
        "total_supply": 2_000_000,
        "ath": 10.0,
        "ath_change_percentage": -80.0,
        **extra,
    }


def _provider(pages: dict[int, list[dict]]) -> CoinGeckoValuationProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == MARKETS_PATH
        page = int(request.url.params.get("page", 1))
        return httpx.Response(200, json=pages.get(page, []))

    cfg = ProviderSettings(valuation="coingecko", valuation_pages=len(pages))
    provider = CoinGeckoValuationProvider(cfg, clock=FrozenClock(FIXED_NOW_MS))
    provider.http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider.http.retry_base_delay = 0.001
    return provider


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


async def test_fields_are_mapped_onto_the_valuation_type():
    provider = _provider({1: [_row("BTC", 1.2e12, 1)]})
    val = (await provider.get_valuations(["BTC"]))["BTC"]

    assert val.market_cap_usd == 1.2e12
    assert val.fully_diluted_valuation_usd == 2.4e12
    assert val.rank == 1
    assert val.circulating_ratio == 0.5
    assert val.ath_change_pct == -80.0
    assert val.provenance.source == "coingecko"
    await provider.close()


async def test_symbols_are_matched_case_insensitively_and_returned_uppercase():
    provider = _provider({1: [_row("pepe", 4e9, 30)]})
    out = await provider.get_valuations(["pepe"])
    assert out["PEPE"].symbol == "PEPE"
    await provider.close()


async def test_a_row_without_a_ticker_is_skipped_rather_than_guessed():
    provider = _provider({1: [{"id": "x", "market_cap": 1e9}, _row("SOL", 8e10, 5)]})
    out = await provider.get_valuations(["SOL"])
    assert set(out) == {"SOL"}
    await provider.close()


async def test_an_unexpected_payload_shape_raises_instead_of_returning_nothing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "rate limited"})

    provider = CoinGeckoValuationProvider(
        ProviderSettings(valuation="coingecko"), clock=FrozenClock(FIXED_NOW_MS)
    )
    provider.http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(SourceUnavailable):
        await provider.get_valuations(["BTC"])
    await provider.close()


# --------------------------------------------------------------------------- #
# The two failure modes that would produce confident nonsense
# --------------------------------------------------------------------------- #


async def test_a_duplicate_ticker_keeps_the_largest_and_flags_the_ambiguity():
    """Several assets trade as SOL. Picking the wrong one is off by orders of magnitude."""
    provider = _provider({1: [_row("SOL", 8e10, 5), _row("SOL", 1.2e6, 480)]})
    val = (await provider.get_valuations(["SOL"]))["SOL"]

    assert val.market_cap_usd == 8e10  # ranked descending, so the first is the largest
    assert val.ambiguous_symbol is True
    await provider.close()


async def test_an_unranked_asset_gets_an_upper_bound_never_a_market_cap():
    provider = _provider({1: [_row("BTC", 1.2e12, 1), _row("TINY", 5_000_000, 500)]})
    out = await provider.get_valuations(["BTC", "NEWCOIN"])

    unranked = out["NEWCOIN"]
    assert unranked.market_cap_usd is None  # never invented
    assert unranked.market_cap_upper_bound_usd == 5_000_000  # smallest cap in the ranking
    assert "upper bound" in unranked.provenance.note
    await provider.close()


# --------------------------------------------------------------------------- #
# Paging and caching
# --------------------------------------------------------------------------- #


async def test_every_configured_page_is_read():
    provider = _provider({1: [_row("BTC", 1.2e12, 1)], 2: [_row("WIF", 1.5e9, 260)]})
    out = await provider.get_valuations(["BTC", "WIF"])
    assert out["WIF"].market_cap_usd == 1.5e9
    await provider.close()


async def test_the_catalogue_is_fetched_once_and_reused_inside_its_ttl():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[_row("BTC", 1.2e12, 1)])

    cfg = ProviderSettings(valuation="coingecko", valuation_pages=1, valuation_ttl_seconds=3600)
    provider = CoinGeckoValuationProvider(cfg, clock=FrozenClock(FIXED_NOW_MS))
    provider.http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await provider.get_valuations(["BTC"])
    await provider.get_valuations(["BTC"])
    assert calls["n"] == 1  # market caps do not move between two scans
    await provider.close()


async def test_health_reports_the_cached_size_rather_than_asserting_availability():
    provider = _provider({1: [_row("BTC", 1.2e12, 1)]})
    health = await provider.health()
    assert health.available is True
    assert "1 ranked assets" in health.detail
    await provider.close()


# --------------------------------------------------------------------------- #
# The scanner does not depend on it
# --------------------------------------------------------------------------- #


async def test_a_scan_completes_normally_when_the_valuation_source_fails():
    from cryptopulse.config.settings import CryptoPulseSettings
    from cryptopulse.providers.fixture import FixtureProvider
    from cryptopulse.scanner.cex import CexScanner

    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.scanner.universe = "robinhood"
    settings.database.url = "sqlite:///:memory:"

    clock = FrozenClock(FIXED_NOW_MS)
    scanner = CexScanner(settings, provider=FixtureProvider(clock=clock), clock=clock)

    class Broken:
        name = "broken"

        async def get_valuations(self, bases):
            raise SourceUnavailable("coingecko: down")

        async def close(self):
            return None

    scanner.valuation_provider = Broken()
    report = await scanner.scan()

    assert report.succeeded > 5
    assert any("valuation source unavailable" in n for n in report.notes)
    # Capacity is unknown, and the reading says so rather than assuming a cap.
    reading = next(r.moonshot for r in report.results if r.moonshot is not None)
    assert reading.capacity is None
    await scanner.close()
