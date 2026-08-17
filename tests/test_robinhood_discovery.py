"""Robinhood Chain token discovery: the GeckoTerminal parser and the search.

The payloads below follow the contract established from three agreeing
readings — `geckoterminal-api` 0.9.0 read off PyPI, GeckoTerminal's published
docs and changelogs, and a third-party schema reference. What no test here can
prove is that api.geckoterminal.com answers this way today: the host is refused
by this environment's egress policy, exactly like Binance and the RPC.

The two failure modes these tests exist for are the ones that would be
invisible downstream: a missing figure becoming 0, and the new token being read
off the wrong side of the pair.
"""

from __future__ import annotations

import json

import httpx
import pytest

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.core.errors import DataUnavailable
from cryptopulse.hunter.robinhood_discovery import (
    AGE_BUCKETS,
    bucket_for,
    discover_new_tokens,
)
from cryptopulse.providers.geckoterminal import GeckoTerminalClient, parse_pool
from tests.conftest import FIXED_NOW_MS

pytestmark = pytest.mark.asyncio

NOW_S = FIXED_NOW_MS // 1000


def iso(seconds_ago: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(NOW_S - seconds_ago, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pool_entry(
    *,
    address: str,
    base_id: str,
    quote_id: str,
    created_ago_s: float | None = 600,
    reserve: str | None = "34000.5",
    base_price: str | None = "0.0004212",
    quote_price: str | None = "3211.44",
    volume: dict | None = None,
    transactions: dict | None = None,
    name: str = "XYZ / WETH 1%",
) -> dict:
    attrs = {
        "name": name,
        "address": address,
        "base_token_price_usd": base_price,
        "quote_token_price_usd": quote_price,
        "reserve_in_usd": reserve,
        "fdv_usd": "82000",
        "market_cap_usd": None,           # commonly null for a fresh token
        "pool_created_at": iso(created_ago_s) if created_ago_s is not None else None,
        "volume_usd": volume if volume is not None else {"h24": "51000", "h6": "40000", "h1": "12000", "m5": "2000"},
        "price_change_percentage": {"h24": "180.4", "h6": "90.1", "h1": "22.5", "m5": "3.1"},
        "transactions": transactions if transactions is not None else {
            "h24": {"buys": 1250, "sells": 980, "buyers": 890, "sellers": 720},
            "h6": {"buys": 320, "sells": 280, "buyers": 210, "sellers": 180},
            "h1": {"buys": 52, "sells": 41, "buyers": 38, "sellers": 29},
            "m5": {"buys": 5, "sells": 3, "buyers": 4, "sellers": 2},
        },
    }
    return {
        "id": f"robinhood_{address}",
        "type": "pool",
        "attributes": attrs,
        "relationships": {
            "base_token": {"data": {"id": base_id, "type": "token"}},
            "quote_token": {"data": {"id": quote_id, "type": "token"}},
            "dex": {"data": {"id": "uniswap-v4", "type": "dex"}},
        },
    }


def token_included(token_id: str, address: str, symbol: str, name: str) -> dict:
    return {
        "id": token_id,
        "type": "token",
        "attributes": {"address": address, "symbol": symbol, "name": name},
    }


XYZ = "0x" + "ab" * 20
WETH = "0x" + "ee" * 20
FOO = "0x" + "cd" * 20


def envelope(entries: list[dict], included: list[dict]) -> dict:
    return {"data": entries, "included": included}


def _client(handler, **overrides) -> GeckoTerminalClient:
    settings = RobinhoodSettings(**overrides)
    client = GeckoTerminalClient(settings, clock=FrozenClock(FIXED_NOW_MS))
    client.http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.http.retry_base_delay = 0.001
    return client


def one_page(payload: dict):
    def handler(request):
        return httpx.Response(200, json=payload)

    return handler


STD_INCLUDED = [
    token_included("robinhood_" + XYZ, XYZ, "XYZ", "Xyz Token"),
    token_included("robinhood_" + WETH, WETH, "WETH", "Wrapped Ether"),
    token_included("robinhood_" + FOO, FOO, "FOO", "Foo Token"),
]


# --------------------------------------------------------------------------- #
# Parsing: the two mistakes that would be invisible downstream
# --------------------------------------------------------------------------- #


async def test_a_missing_number_is_none_never_zero():
    """Unknown liquidity and zero liquidity are opposite facts. Turning the
    first into the second is how a dead pool passes a liquidity floor."""
    included = {t["id"]: t for t in STD_INCLUDED}
    entry = pool_entry(
        address="0x1", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH,
        reserve=None, base_price="",
    )
    pool = parse_pool(entry, included)
    assert pool is not None
    assert pool.liquidity_usd is None
    assert pool.base_price_usd is None
    assert pool.market_cap_usd is None
    assert 0.0 not in (pool.liquidity_usd, pool.base_price_usd, pool.market_cap_usd)


async def test_numbers_arrive_as_strings_and_are_parsed():
    included = {t["id"]: t for t in STD_INCLUDED}
    pool = parse_pool(
        pool_entry(address="0x1", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH),
        included,
    )
    assert pool.liquidity_usd == pytest.approx(34000.5)
    assert pool.fdv_usd == pytest.approx(82000.0)
    assert pool.volume_usd["h1"] == pytest.approx(12000.0)
    assert pool.price_change_pct["m5"] == pytest.approx(3.1)
    assert pool.buyers["h1"] == 38
    assert pool.sells["h24"] == 980


async def test_the_new_token_is_read_off_the_correct_side():
    """Address ordering decides base vs quote, so a new token is the quote side
    about half the time. Reading base_token blindly would report WETH as the
    discovery, priced at WETH's price."""
    included = {t["id"]: t for t in STD_INCLUDED}

    normal = parse_pool(
        pool_entry(address="0x1", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH),
        included,
    )
    assert normal.token.symbol == "XYZ"
    assert normal.token_price_usd == pytest.approx(0.0004212)

    flipped = parse_pool(
        pool_entry(address="0x2", base_id="robinhood_" + WETH, quote_id="robinhood_" + XYZ),
        included,
    )
    assert flipped.token.symbol == "XYZ", "WETH must never be the discovery"
    assert flipped.token_price_usd == pytest.approx(3211.44), "price follows the side"


async def test_an_unparseable_created_at_is_none_not_now():
    included = {t["id"]: t for t in STD_INCLUDED}
    entry = pool_entry(address="0x1", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH)
    entry["attributes"]["pool_created_at"] = "not a date"
    pool = parse_pool(entry, included)
    assert pool.created_at_ms is None
    assert pool.age_seconds(now_ms=FIXED_NOW_MS) is None


async def test_a_malformed_row_is_skipped_not_fatal():
    """One bad row must not lose the other thirty-nine."""
    payload = envelope(
        [
            {"id": "x", "type": "pool"},                        # no attributes
            {"id": "y", "type": "pool", "attributes": {}},      # no address
            pool_entry(address="0x3", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH),
        ],
        STD_INCLUDED,
    )
    client = _client(one_page(payload))
    pools = await client.new_pools()
    assert len(pools) == 1
    assert pools[0].pool_address == "0x3"
    await client.close()


async def test_a_json_api_error_is_typed_with_its_message():
    def handler(request):
        return httpx.Response(200, json={"errors": [{"title": "Network not found"}]})

    client = _client(handler)
    with pytest.raises(DataUnavailable, match="Network not found"):
        await client.new_pools()
    await client.close()


async def test_the_request_asks_for_the_right_network_and_includes():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=envelope([], []))

    client = _client(handler)
    await client.new_pools(page=2)
    assert "/networks/robinhood/new_pools" in seen["url"]
    assert "include=base_token%2Cquote_token%2Cdex" in seen["url"] or "include=base_token,quote_token,dex" in seen["url"]
    assert "page=2" in seen["url"]
    await client.close()


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


async def test_age_buckets_are_narrowest_first():
    assert bucket_for(60) == "lt_15m"
    assert bucket_for(20 * 60) == "lt_30m"
    assert bucket_for(45 * 60) == "lt_1h"
    assert bucket_for(3 * 3600) == "lt_6h"
    assert bucket_for(20 * 3600) == "lt_24h"
    assert bucket_for(30 * 3600) is None
    assert [b[0] for b in AGE_BUCKETS] == ["lt_15m", "lt_30m", "lt_1h", "lt_6h", "lt_24h"]


async def test_an_unknown_age_is_never_filed_as_brand_new():
    """The dangerous default: a token with no creation time landing in the
    '< 15 min' list, which is the list the user acts on fastest."""
    assert bucket_for(None) is None


async def test_discovery_groups_pools_by_contract_address():
    payload = envelope(
        [
            pool_entry(address="0xpoolA", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH,
                       created_ago_s=600, reserve="10000"),
            pool_entry(address="0xpoolB", base_id="robinhood_" + WETH, quote_id="robinhood_" + XYZ,
                       created_ago_s=1200, reserve="5000"),
            pool_entry(address="0xpoolC", base_id="robinhood_" + FOO, quote_id="robinhood_" + WETH,
                       created_ago_s=300, reserve="20000"),
        ],
        STD_INCLUDED,
    )
    client = _client(one_page(payload), discovery_pages=1)
    report = await discover_new_tokens(
        client, client.settings, clock=FrozenClock(FIXED_NOW_MS)
    )
    assert len(report.candidates) == 2
    xyz = next(c for c in report.candidates if c.symbol == "XYZ")
    assert len(xyz.pools) == 2
    assert xyz.liquidity_usd == pytest.approx(15000.0)
    assert xyz.liquidity_is_partial is False
    # The oldest pool dates the token: a fresh second pool does not rejuvenate it.
    assert xyz.pool_age_seconds == pytest.approx(1200, abs=2)
    await client.close()


async def test_a_partial_liquidity_total_says_so():
    payload = envelope(
        [
            pool_entry(address="0xa", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH, reserve="10000"),
            pool_entry(address="0xb", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH, reserve=None),
        ],
        STD_INCLUDED,
    )
    client = _client(one_page(payload), discovery_pages=1)
    report = await discover_new_tokens(client, client.settings, clock=FrozenClock(FIXED_NOW_MS))
    xyz = report.candidates[0]
    assert xyz.liquidity_usd == pytest.approx(10000.0)
    assert xyz.liquidity_is_partial is True, "a total from some pools is not the token's liquidity"
    await client.close()


async def test_old_and_illiquid_pools_are_filtered_and_counted():
    payload = envelope(
        [
            pool_entry(address="0xa", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH,
                       created_ago_s=48 * 3600),
            pool_entry(address="0xb", base_id="robinhood_" + FOO, quote_id="robinhood_" + WETH,
                       created_ago_s=600, reserve="12.5"),
        ],
        STD_INCLUDED,
    )
    client = _client(one_page(payload), discovery_pages=1)
    report = await discover_new_tokens(client, client.settings, clock=FrozenClock(FIXED_NOW_MS))
    assert report.candidates == []
    assert report.filtered_too_old == 1
    assert report.filtered_illiquid == 1
    await client.close()


async def test_the_report_states_its_coverage_rather_than_implying_a_census():
    payload = envelope(
        [pool_entry(address="0xa", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH)],
        STD_INCLUDED,
    )
    client = _client(one_page(payload), discovery_pages=1)
    report = await discover_new_tokens(client, client.settings, clock=FrozenClock(FIXED_NOW_MS))
    d = report.to_dict()
    assert d["pools_seen"] == 1
    assert d["requests"] == 1
    assert "recensement" in d["coverage_note"]
    await client.close()


async def test_a_failing_page_keeps_what_the_earlier_pages_returned():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=envelope(
                [pool_entry(address="0xa", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH)],
                STD_INCLUDED,
            ))
        return httpx.Response(500, json={})

    client = _client(handler, discovery_pages=3)
    report = await discover_new_tokens(client, client.settings, clock=FrozenClock(FIXED_NOW_MS))
    assert len(report.candidates) == 1
    assert report.errors, "the failure must be reported, not swallowed"
    await client.close()


async def test_buy_sell_ratio_with_no_sells_is_undefined_not_infinite():
    payload = envelope(
        [pool_entry(
            address="0xa", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH,
            transactions={"h1": {"buys": 40, "sells": 0, "buyers": 30, "sellers": 0}},
        )],
        STD_INCLUDED,
    )
    client = _client(one_page(payload), discovery_pages=1)
    report = await discover_new_tokens(client, client.settings, clock=FrozenClock(FIXED_NOW_MS))
    assert report.candidates[0].buy_sell_ratio_h1 is None
    await client.close()


async def test_volume_acceleration_is_arithmetic_on_two_returned_figures():
    """(m5 * 12) / h1 — the current rate against the hourly average. Above 1
    means it is accelerating; it is not a prediction and is None when either
    side is missing."""
    payload = envelope(
        [pool_entry(
            address="0xa", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH,
            volume={"m5": "1000", "h1": "6000"},
        )],
        STD_INCLUDED,
    )
    client = _client(one_page(payload), discovery_pages=1)
    report = await discover_new_tokens(client, client.settings, clock=FrozenClock(FIXED_NOW_MS))
    assert report.candidates[0].volume_acceleration == pytest.approx(2.0)
    await client.close()


async def test_volume_acceleration_is_none_without_both_sides():
    payload = envelope(
        [pool_entry(address="0xa", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH,
                    volume={"h1": "6000"})],
        STD_INCLUDED,
    )
    client = _client(one_page(payload), discovery_pages=1)
    report = await discover_new_tokens(client, client.settings, clock=FrozenClock(FIXED_NOW_MS))
    assert report.candidates[0].volume_acceleration is None
    await client.close()


async def test_every_row_carries_its_market_identity():
    """Spec §4: provider, chain and contract address travel with the data, so a
    Robinhood row can never be mistaken for a Binance one."""
    payload = envelope(
        [pool_entry(address="0xa", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH)],
        STD_INCLUDED,
    )
    client = _client(one_page(payload), discovery_pages=1)
    report = await discover_new_tokens(client, client.settings, clock=FrozenClock(FIXED_NOW_MS))
    row = report.to_dict()["tokens"][0]
    assert row["provider"] == "ROBINHOOD_CHAIN"
    assert row["chain_id"] == 4663
    assert row["contract_address"] == XYZ
    # Pool age is measured; token age is not knowable from this source.
    assert row["pool_age_seconds"] is not None
    assert row["token_age_seconds"] is None
    await client.close()


async def test_the_payload_shape_matches_the_documented_contract():
    """A guard on the fixture itself: if this ever drifts from the field names
    established from the three references, the tests above would be proving a
    parser against a contract nobody uses."""
    entry = pool_entry(address="0xa", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH)
    attrs = json.loads(json.dumps(entry))["attributes"]
    for key in (
        "name", "address", "base_token_price_usd", "quote_token_price_usd",
        "reserve_in_usd", "fdv_usd", "market_cap_usd", "pool_created_at",
        "volume_usd", "price_change_percentage", "transactions",
    ):
        assert key in attrs, key
    assert set(attrs["transactions"]["h1"]) == {"buys", "sells", "buyers", "sellers"}


async def test_a_pool_between_two_quote_assets_is_not_a_discovery():
    """Found by running the search against a stubbed indexer: a WETH/USDC pool
    has no new side, so `token` returned WETH and the screen announced a blue
    chip as a fresh find. Infrastructure pools are filtered and counted."""
    usdc = "0x" + "99" * 20
    included = STD_INCLUDED + [token_included("robinhood_" + usdc, usdc, "USDC", "USD Coin")]
    payload = envelope(
        [
            pool_entry(address="0xinfra", base_id="robinhood_" + WETH, quote_id="robinhood_" + usdc),
            pool_entry(address="0xreal", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH),
        ],
        included,
    )
    client = _client(one_page(payload), discovery_pages=1)
    report = await discover_new_tokens(client, client.settings, clock=FrozenClock(FIXED_NOW_MS))
    assert [c.symbol for c in report.candidates] == ["XYZ"]
    assert report.filtered_not_a_discovery == 1
    assert "WETH" not in [c.symbol for c in report.candidates]
    await client.close()


# --------------------------------------------------------------------------- #
# Safety attached to discovery
# --------------------------------------------------------------------------- #


class FakeGoPlus:
    """Records what it was asked, returns what it was told to."""

    def __init__(self, answers: dict, fail: bool = False):
        self.answers = answers
        self.fail = fail
        self.calls: list[list[str]] = []

    async def token_security(self, addresses, *, chain_id):
        from cryptopulse.providers.goplus import parse_token_security

        self.calls.append(list(addresses))
        if self.fail:
            raise RuntimeError("goplus down")
        return {
            a.lower(): parse_token_security(a.lower(), self.answers[a.lower()])
            for a in addresses
            if a.lower() in self.answers
        }


CLEAN = {
    "is_honeypot": "0", "cannot_sell_all": "0", "cannot_buy": "0",
    "buy_tax": "0", "sell_tax": "0", "is_mintable": "0",
    "owner_change_balance": "0", "can_take_back_ownership": "0",
    "hidden_owner": "0", "selfdestruct": "0", "is_proxy": "0",
    "is_blacklisted": "0", "transfer_pausable": "0", "is_open_source": "1",
    "slippage_modifiable": "0", "owner_percent": "0", "creator_percent": "0.01",
    "holder_count": "900", "lp_holder_count": "2",
    "holders": [{"address": "0xa", "percent": "0.04", "is_locked": "0", "is_contract": "0"}],
    "lp_holders": [{"address": "0x000000000000000000000000000000000000dead",
                    "percent": "0.95", "is_locked": "0", "is_contract": "0"}],
}


async def _discovered(**over):
    payload = envelope(
        [pool_entry(address="0xa", base_id="robinhood_" + XYZ, quote_id="robinhood_" + WETH),
         pool_entry(address="0xb", base_id="robinhood_" + FOO, quote_id="robinhood_" + WETH)],
        STD_INCLUDED,
    )
    client = _client(one_page(payload), discovery_pages=1, **over)
    report = await discover_new_tokens(client, client.settings, clock=FrozenClock(FIXED_NOW_MS))
    await client.close()
    return report, RobinhoodSettings(**over)


async def test_safety_is_attached_to_every_candidate():
    from cryptopulse.hunter.robinhood_discovery import attach_safety

    report, settings = await _discovered()
    gp = FakeGoPlus({XYZ.lower(): CLEAN, FOO.lower(): {**CLEAN, "is_honeypot": "1"}})
    await attach_safety(report, gp, settings)

    by_symbol = {c.symbol: c for c in report.candidates}
    assert by_symbol["XYZ"].safety.hard_veto is False
    assert by_symbol["FOO"].safety.hard_veto is True
    assert report.safety_analysed == 2


async def test_a_token_the_security_source_ignores_is_vetoed_not_cleared():
    """The most dangerous default in this whole phase: no answer becoming a
    pass. A brand-new contract is exactly what GoPlus has not analysed yet."""
    from cryptopulse.hunter.robinhood_discovery import attach_safety

    report, settings = await _discovered()
    gp = FakeGoPlus({XYZ.lower(): CLEAN})   # nothing for FOO
    await attach_safety(report, gp, settings)

    foo = next(c for c in report.candidates if c.symbol == "FOO")
    assert foo.safety is not None, "a missing verdict must still be a verdict object"
    assert foo.safety.analysed is False
    assert foo.safety.score is None
    assert foo.safety.hard_veto is True


async def test_a_failing_security_source_vetoes_rather_than_clears():
    from cryptopulse.hunter.robinhood_discovery import attach_safety

    report, settings = await _discovered()
    await attach_safety(report, FakeGoPlus({}, fail=True), settings)
    assert all(c.safety.hard_veto for c in report.candidates)
    assert report.errors


async def test_safety_is_batched_and_states_its_request_cost():
    """One request per batch, not per token: a 30-per-minute budget cannot
    survive a per-token loop, and a search that quietly spent it would be
    discovered as a ban rather than as a number."""
    from cryptopulse.hunter.robinhood_discovery import attach_safety

    report, settings = await _discovered(goplus_batch_size=30)
    gp = FakeGoPlus({XYZ.lower(): CLEAN, FOO.lower(): CLEAN})
    await attach_safety(report, gp, settings)
    assert len(gp.calls) == 1
    assert report.safety_requests == 1
    assert report.to_dict()["safety_requests"] == 1


async def test_the_row_carries_its_veto_for_the_ui():
    from cryptopulse.hunter.robinhood_discovery import attach_safety

    report, settings = await _discovered()
    await attach_safety(report, FakeGoPlus({XYZ.lower(): {**CLEAN, "is_honeypot": "1"}}), settings)
    rows = {r["symbol"]: r for r in report.to_dict()["tokens"]}
    assert rows["XYZ"]["hard_veto"] is True
    assert rows["XYZ"]["safety"]["rug_risk"] == "CRITICAL"
    assert report.to_dict()["vetoed"] == 2   # FOO had no data, so it is vetoed too


async def test_explosion_is_scored_after_safety_never_before():
    """A row must never carry a high explosion score and a veto at the same
    time: the veto zeroes it, so the order of the two passes is load-bearing."""
    from cryptopulse.hunter.robinhood_discovery import attach_safety

    report, settings = await _discovered()
    gp = FakeGoPlus({XYZ.lower(): {**CLEAN, "is_honeypot": "1"}, FOO.lower(): CLEAN})
    await attach_safety(report, gp, settings)

    rows = {r["symbol"]: r for r in report.to_dict()["tokens"]}
    assert rows["XYZ"]["hard_veto"] is True
    assert rows["XYZ"]["explosion"]["score"] == 0.0
    assert rows["XYZ"]["explosion"]["vetoed"] is True
    assert rows["FOO"]["explosion"] is not None
