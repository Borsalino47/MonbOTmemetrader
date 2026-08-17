"""Robinhood Chain RPC layer + chain doctor.

Everything here runs against a mocked JSON-RPC endpoint shaped like the
Ethereum standard. What no test in this file can prove: that the real
`rpc.mainnet.chain.robinhood.com` behaves this way — this environment's network
policy refuses the host (403 on CONNECT, verified). `cryptopulse
doctor-robinhood` on the phone is what settles that, exactly as `doctor` did
for Binance.
"""

from __future__ import annotations

import json

import httpx
import pytest

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.core.errors import DataUnavailable, SourceUnavailable
from cryptopulse.providers.robinhood import (
    ROBINHOOD_CHAIN_ID,
    ROBINHOOD_PROVIDER_ID,
    RobinhoodRpc,
)
from cryptopulse.providers.robinhood_verify import (
    PRESENTATION,
    ChainState,
    verify_robinhood_chain,
)
from tests.conftest import FIXED_NOW_MS

pytestmark = pytest.mark.asyncio

# A block timestamp coherent with the frozen clock (5 s ago).
FRESH_TS = FIXED_NOW_MS // 1000 - 5


def _rpc(handler, **overrides) -> RobinhoodRpc:
    settings = RobinhoodSettings(**overrides)
    rpc = RobinhoodRpc(settings, clock=FrozenClock(FIXED_NOW_MS))
    rpc.http._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rpc.http.retry_base_delay = 0.001
    return rpc


async def _noop_wait() -> None:
    return None


def _result(request, value) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": value})


class ChainSim:
    """A minimal healthy chain: right id, advancing blocks, fresh timestamps."""

    def __init__(self, chain_id: int = ROBINHOOD_CHAIN_ID, advancing: bool = True, block_ts: int = FRESH_TS):
        self.chain_id = chain_id
        self.advancing = advancing
        self.block_ts = block_ts
        self.block = 4_200_000
        self.calls: list[str] = []

    def __call__(self, request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        self.calls.append(method)
        if method == "eth_chainId":
            return _result(request, hex(self.chain_id))
        if method == "eth_blockNumber":
            if self.advancing:
                self.block += 17
            return _result(request, hex(self.block))
        if method == "eth_getBlockByNumber":
            return _result(
                request,
                {"number": hex(self.block), "timestamp": hex(self.block_ts), "hash": "0xabc"},
            )
        if method == "eth_getLogs":
            return _result(request, [{"address": "0x1", "topics": []}] * 3)
        if method == "eth_getCode":
            return _result(request, "0x600160")
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "method not found"}},
        )


# --------------------------------------------------------------------------- #
# The RPC envelope
# --------------------------------------------------------------------------- #


async def test_chain_id_parses_the_hex_quantity():
    rpc = _rpc(ChainSim())
    assert await rpc.chain_id() == 4663
    await rpc.close()


async def test_an_error_inside_http_200_is_raised_never_returned():
    """JSON-RPC's Kraken trap: the failure arrives with a 200 status."""

    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32000, "message": "header not found"}},
        )

    rpc = _rpc(handler)
    with pytest.raises(SourceUnavailable, match="header not found"):
        await rpc.latest_block()
    await rpc.close()


async def test_a_malformed_request_error_is_our_fault_not_the_chains():
    """-326xx codes must not read as 'the chain is down'."""

    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32602, "message": "invalid params"}},
        )

    rpc = _rpc(handler)
    with pytest.raises(DataUnavailable, match="invalid params"):
        await rpc.get_logs(1, 2)
    await rpc.close()


async def test_an_envelope_with_neither_result_nor_error_is_not_json_rpc():
    """A captive portal or broken proxy answering 200 with junk must be named."""

    def handler(request):
        return httpx.Response(200, json={"hello": "world"})

    rpc = _rpc(handler)
    with pytest.raises(SourceUnavailable, match="no result"):
        await rpc.chain_id()
    await rpc.close()


async def test_a_non_hex_quantity_is_a_typed_error_not_a_crash():
    def handler(request):
        return _result(request, {"unexpected": "object"})

    rpc = _rpc(handler)
    with pytest.raises(DataUnavailable, match="hex quantity"):
        await rpc.chain_id()
    await rpc.close()


async def test_block_timestamps_are_converted_to_milliseconds():
    """The whole project speaks ms; a chain speaking seconds must be converted
    at the boundary, once."""
    rpc = _rpc(ChainSim(block_ts=1_750_000_000))
    block = await rpc.latest_block()
    assert block.timestamp_ms == 1_750_000_000_000
    await rpc.close()


async def test_the_endpoint_host_never_includes_the_path():
    """An Alchemy-style URL carries its API key in the path — the displayable
    identity must stop at the host."""
    rpc = _rpc(ChainSim(), rpc_url="https://rh.g.alchemy.com/v2/SECRET-KEY-123")
    assert rpc.endpoint_host == "rh.g.alchemy.com"
    assert "SECRET" not in rpc.endpoint_host
    await rpc.close()


# --------------------------------------------------------------------------- #
# The doctor
# --------------------------------------------------------------------------- #


async def test_a_healthy_chain_is_verified():
    rpc = _rpc(ChainSim())
    result = await verify_robinhood_chain(rpc, clock=FrozenClock(FIXED_NOW_MS), progression_wait=_noop_wait)
    assert result.state is ChainState.VERIFIED
    assert result.verified
    assert result.chain_id == 4663
    assert result.latest_block > 0
    assert all(c.ok for c in result.checks)
    await rpc.close()


async def test_wrong_chain_id_is_failed_with_the_configuration_named():
    """Ethereum mainnet answering means the URL in .env points at the wrong
    network — the single most likely misconfiguration."""
    rpc = _rpc(ChainSim(chain_id=1))
    result = await verify_robinhood_chain(rpc, clock=FrozenClock(FIXED_NOW_MS), progression_wait=_noop_wait)
    assert result.state is ChainState.FAILED
    assert not result.verified
    assert "chaîne 1" in " ".join(c.detail for c in result.checks)
    assert "CP_ROBINHOOD_RPC_URL" in " ".join(c.detail for c in result.checks)
    await rpc.close()


async def test_an_unreachable_rpc_is_failed_not_crashed():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    rpc = _rpc(handler)
    result = await verify_robinhood_chain(rpc, clock=FrozenClock(FIXED_NOW_MS), progression_wait=_noop_wait)
    assert result.state is ChainState.FAILED
    assert result.error is not None
    await rpc.close()


async def test_stalled_blocks_are_partial_not_failed():
    """Reachable + right chain + frozen head: the screen must say which half is
    broken, not show the same red as 'no RPC at all'."""
    rpc = _rpc(ChainSim(advancing=False))
    result = await verify_robinhood_chain(rpc, clock=FrozenClock(FIXED_NOW_MS), progression_wait=_noop_wait)
    assert result.state is ChainState.PARTIAL
    assert not result.verified, "PARTIAL must license nothing"
    failed = [c.name for c in result.checks if not c.ok]
    assert failed == ["les blocs avancent"]
    await rpc.close()


async def test_a_stale_block_timestamp_is_partial():
    """An RPC serving ten-minute-old state answers correctly and is still not
    a live view of the chain."""
    rpc = _rpc(ChainSim(block_ts=FIXED_NOW_MS // 1000 - 600))
    result = await verify_robinhood_chain(rpc, clock=FrozenClock(FIXED_NOW_MS), progression_wait=_noop_wait)
    assert result.state is ChainState.PARTIAL
    assert any("horodatage" in c.name and not c.ok for c in result.checks)
    await rpc.close()


async def test_the_known_contract_check_is_skipped_when_unconfigured():
    rpc = _rpc(ChainSim())
    result = await verify_robinhood_chain(rpc, clock=FrozenClock(FIXED_NOW_MS), progression_wait=_noop_wait)
    assert not any("contrat connu" in c.name for c in result.checks)
    await rpc.close()


async def test_a_configured_contract_with_no_code_is_partial():
    """`eth_getCode` returning 0x means the address holds nothing — checking a
    guessed address would produce exactly this, which is why none is guessed."""
    sim = ChainSim()

    def handler(request):
        body = json.loads(request.content)
        if body["method"] == "eth_getCode":
            return _result(request, "0x")
        return sim(request)

    rpc = _rpc(handler, known_contract_address="0x" + "11" * 20)
    result = await verify_robinhood_chain(rpc, clock=FrozenClock(FIXED_NOW_MS), progression_wait=_noop_wait)
    assert result.state is ChainState.PARTIAL
    assert any("contrat connu" in c.name and not c.ok for c in result.checks)
    await rpc.close()


async def test_the_verdict_is_independent_of_binance_in_name_and_content():
    """Spec §6: never declare Robinhood live because Binance works. Structural
    guarantee here: no label mentions Binance, and the doctor calls only
    eth_* / web3_* methods."""
    for _, label in PRESENTATION.values():
        assert "BINANCE" not in label.upper()
    sim = ChainSim()
    rpc = _rpc(sim)
    await verify_robinhood_chain(rpc, clock=FrozenClock(FIXED_NOW_MS), progression_wait=_noop_wait)
    assert all(m.startswith(("eth_", "web3_")) for m in sim.calls)
    await rpc.close()


async def test_to_dict_carries_the_full_story():
    rpc = _rpc(ChainSim())
    result = await verify_robinhood_chain(rpc, clock=FrozenClock(FIXED_NOW_MS), progression_wait=_noop_wait)
    d = result.to_dict()
    assert d["state"] == "VERIFIED"
    assert d["emoji"] == "🟢"
    assert d["label_fr"] == "ROBINHOOD CHAIN LIVE VERIFIED"
    assert d["chain_id"] == 4663
    assert d["checked_at_ms"] == FIXED_NOW_MS
    assert d["total"] == len(d["checks"]) > 0
    assert d["note"]
    await rpc.close()


async def test_provider_id_is_the_multi_market_tag():
    """The tag future rows will carry. Pinned so a rename is a visible,
    deliberate migration rather than a drive-by edit."""
    assert ROBINHOOD_PROVIDER_ID == "ROBINHOOD_CHAIN"
    assert ROBINHOOD_CHAIN_ID == 4663
