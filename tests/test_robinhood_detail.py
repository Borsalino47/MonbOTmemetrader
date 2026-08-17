"""DexScreener as a second opinion, and what happens when the two disagree.

Contract cross-checked against `dexscreener` 1.3 from PyPI. The host is refused
by this environment; `doctor-robinhood` on a machine with egress is what proves
it live.

The rule these tests defend: two sources are reported side by side and never
merged. An average of two prices 40 % apart describes neither and destroys the
one signal that mattered.
"""

from __future__ import annotations

import httpx
import pytest

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.hunter.robinhood_detail import (
    Agreement,
    build_detail,
    cross_check,
)
from cryptopulse.hunter.robinhood_discovery import TokenCandidate
from cryptopulse.providers.dexscreener import DexScreenerClient, parse_pair
from tests.conftest import FIXED_NOW_MS

pytestmark = pytest.mark.asyncio

SETTINGS = RobinhoodSettings()
XYZ = "0x" + "ab" * 20


def ds_pair(
    *, address="0xpair", price="0.00042", liquidity=34000.5, fdv=82000.0,
    chain="robinhood", created_ms=FIXED_NOW_MS - 600_000,
) -> dict:
    return {
        "chainId": chain,
        "dexId": "uniswap",
        "url": "https://dexscreener.com/robinhood/0xpair",
        "pairAddress": address,
        "baseToken": {"address": XYZ, "name": "Xyz", "symbol": "XYZ"},
        "quoteToken": {"address": "0x" + "ee" * 20, "name": "WETH", "symbol": "WETH"},
        "priceNative": "0.00000013",
        "priceUsd": price,
        "txns": {"h1": {"buys": 52, "sells": 41}, "m5": {"buys": 5, "sells": 3}},
        "volume": {"h24": 51000, "h1": 12000, "m5": 2000},
        "priceChange": {"h1": 22.5, "m5": 3.1},
        "liquidity": {"usd": liquidity, "base": 1000000, "quote": 5.2},
        "fdv": fdv,
        "marketCap": None,
        "pairCreatedAt": created_ms,
    }


def candidate(price=0.00042, liquidity=34000.5, fdv=82000.0) -> TokenCandidate:
    c = TokenCandidate(address=XYZ, symbol="XYZ", name="Xyz")
    c.price_usd = price
    c.liquidity_usd = liquidity
    c.fdv_usd = fdv
    c.pool_age_seconds = 600.0
    return c


def _client(handler, **over) -> DexScreenerClient:
    client = DexScreenerClient(RobinhoodSettings(**over), clock=FrozenClock(FIXED_NOW_MS))
    client.http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.http.retry_base_delay = 0.001
    return client


def respond(payload):
    def handler(request):
        return httpx.Response(200, json=payload)

    return handler


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


async def test_prices_arrive_as_strings_and_liquidity_as_numbers():
    p = parse_pair(ds_pair())
    assert p.price_usd == pytest.approx(0.00042)
    assert p.liquidity_usd == pytest.approx(34000.5)
    assert p.liquidity_base == pytest.approx(1_000_000)
    assert p.buys["h1"] == 52
    assert p.volume_usd["h24"] == pytest.approx(51000)


async def test_pair_created_at_is_already_milliseconds():
    """DexScreener gives epoch ms; GeckoTerminal gives ISO seconds. Converting
    one like the other is how two ages end up a thousand times apart."""
    p = parse_pair(ds_pair(created_ms=1_750_000_000_000))
    assert p.created_at_ms == 1_750_000_000_000


async def test_a_missing_figure_is_none_never_zero():
    raw = ds_pair()
    raw["liquidity"] = {}
    raw["priceUsd"] = None
    p = parse_pair(raw)
    assert p.liquidity_usd is None
    assert p.price_usd is None


async def test_pairs_on_other_chains_are_dropped():
    """The endpoint answers across all chains. The same address elsewhere would
    otherwise add its liquidity to this chain's total."""
    client = _client(respond({"pairs": [ds_pair(chain="base"), ds_pair(address="0xok")]}))
    pairs = await client.token_pairs(XYZ)
    assert [p.pair_address for p in pairs] == ["0xok"]
    await client.close()


async def test_a_token_nobody_indexed_returns_empty_not_an_error():
    """DexScreener answers `pairs: null` for an unknown token. That is absence,
    not an outage."""
    client = _client(respond({"pairs": None}))
    assert await client.token_pairs(XYZ) == []
    await client.close()


# --------------------------------------------------------------------------- #
# The cross-check
# --------------------------------------------------------------------------- #


async def test_two_agreeing_sources_agree():
    check = cross_check(candidate(), [parse_pair(ds_pair())], SETTINGS)
    assert check.agreement is Agreement.AGREE
    assert check.disagreements == []
    assert check.caveats == []
    assert check.sources_seen == ["DEXSCREENER"] or "DEXSCREENER" in check.sources_seen


async def test_a_price_disagreement_is_surfaced_never_averaged():
    """The rule this module exists for: an average of 0.00042 and 0.00061 would
    describe neither and would hide the fact worth knowing."""
    check = cross_check(candidate(price=0.00042), [parse_pair(ds_pair(price="0.00061"))], SETTINGS)
    assert check.agreement is Agreement.DISAGREE
    price = next(c for c in check.comparisons if c.field == "price_usd")
    # Both values survive; there is no merged field to fall back on.
    assert price.geckoterminal == pytest.approx(0.00042)
    assert price.dexscreener == pytest.approx(0.00061)
    assert price.drift_pct and price.drift_pct > 5
    assert "average" not in price.to_dict()
    assert "value" not in price.to_dict()
    assert check.caveats


async def test_small_differences_are_tolerated():
    """The two sample at slightly different moments; a real mapping error is
    off by orders of magnitude, not by a percent."""
    check = cross_check(candidate(price=1.000), [parse_pair(ds_pair(price="1.02"))], SETTINGS)
    price = next(c for c in check.comparisons if c.field == "price_usd")
    assert price.agreement is Agreement.AGREE


async def test_one_source_only_is_its_own_state():
    """Ordinary for a token minted twenty minutes ago, and not a defect — but
    nothing has been verified, and the caveat says so."""
    check = cross_check(candidate(), [], SETTINGS)
    assert check.agreement is Agreement.SINGLE_SOURCE
    assert any("aucun recoupement" in c for c in check.caveats)


async def test_no_data_anywhere_is_not_agreement():
    empty = TokenCandidate(address=XYZ, symbol=None, name=None)
    check = cross_check(empty, [], SETTINGS)
    assert check.agreement is not Agreement.AGREE


async def test_drift_is_symmetric_between_the_two_sources():
    """Which source is 'reference' is not a question this module may answer;
    dividing by one of them would make the drift depend on that choice."""
    a = cross_check(candidate(price=100.0), [parse_pair(ds_pair(price="50"))], SETTINGS)
    b = cross_check(candidate(price=50.0), [parse_pair(ds_pair(price="100"))], SETTINGS)
    da = next(c for c in a.comparisons if c.field == "price_usd").drift_pct
    db = next(c for c in b.comparisons if c.field == "price_usd").drift_pct
    assert da == pytest.approx(db)


async def test_liquidity_is_summed_per_source_then_compared():
    check = cross_check(
        candidate(liquidity=20000.0),
        [parse_pair(ds_pair(address="0xa", liquidity=12000.0)),
         parse_pair(ds_pair(address="0xb", liquidity=8000.0))],
        SETTINGS,
    )
    liq = next(c for c in check.comparisons if c.field == "liquidity_usd")
    assert liq.dexscreener == pytest.approx(20000.0)
    assert liq.agreement is Agreement.AGREE


# --------------------------------------------------------------------------- #
# The assembled detail
# --------------------------------------------------------------------------- #


async def test_detail_keeps_the_pools_of_each_source_apart():
    client = _client(respond({"pairs": [ds_pair()]}))
    detail = await build_detail(candidate(), client, SETTINGS)
    d = detail.to_dict()
    assert "geckoterminal" in d["pools"] and "dexscreener" in d["pools"]
    assert d["pools"]["dexscreener"][0]["source"] == "DEXSCREENER"
    assert d["crosscheck"]["agreement"] == "AGREE"
    await client.close()


async def test_a_dexscreener_failure_leaves_the_geckoterminal_view_intact():
    def handler(request):
        raise httpx.ConnectError("refused")

    client = _client(handler)
    detail = await build_detail(candidate(), client, SETTINGS)
    assert detail.errors
    assert detail.ds_pairs == []
    # The token is still described, with its single source stated as such.
    assert detail.crosscheck.agreement is Agreement.SINGLE_SOURCE
    assert detail.to_dict()["price_usd"] == pytest.approx(0.00042)
    await client.close()


async def test_the_detail_states_its_request_cost():
    client = _client(respond({"pairs": [ds_pair()]}))
    detail = await build_detail(candidate(), client, SETTINGS)
    assert detail.to_dict()["requests"] == 1
    await client.close()


async def test_the_detail_carries_market_identity():
    client = _client(respond({"pairs": [ds_pair()]}))
    d = (await build_detail(candidate(), client, SETTINGS)).to_dict()
    assert d["provider"] == "ROBINHOOD_CHAIN"
    assert d["chain_id"] == 4663
    assert d["contract_address"] == XYZ
    await client.close()
