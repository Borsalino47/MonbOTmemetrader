"""Robinhood Chain — the JSON-RPC layer and the chain's identity.

Robinhood Chain is an EVM L2 built on Arbitrum Orbit/Nitro (mainnet 2026-07-01),
chain id 4663, ~100 ms blocks, gas in ETH. This module guarantees exactly two
things:

  * every RPC answer is unwrapped from its JSON-RPC envelope, and an in-band
    error is raised as a typed error — never returned as data. JSON-RPC, like
    Kraken, reports failures inside an HTTP 200;
  * nothing here produces a market value. Prices, pools and tokens arrive in
    later phases from the aggregator clients; this layer only proves which
    chain we are talking to and that it is alive.

Written, like the Binance connector before it, in an environment whose network
policy refuses the host (403 on CONNECT, verified). The contract implemented is
the Ethereum JSON-RPC standard — the least exotic API in this project — and the
doctor in `robinhood_verify.py` is what turns IMPLEMENTED into LIVE VERIFIED,
on a machine that can actually reach the chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.errors import DataUnavailable, SourceUnavailable
from cryptopulse.core.logging import get_logger
from cryptopulse.providers.http import HttpClient

log = get_logger("providers.robinhood")

__all__ = [
    "ROBINHOOD_CHAIN_ID",
    "ROBINHOOD_NETWORK",
    "ROBINHOOD_PROVIDER_ID",
    "BlockStamp",
    "RobinhoodRpc",
]

# The chain's identity. 4663 == 0x1237; the doctor refuses any RPC that answers
# with a different id, because a wrong URL in .env would otherwise scan the
# wrong blockchain with perfect confidence.
ROBINHOOD_CHAIN_ID = 4663
ROBINHOOD_NETWORK = "robinhood-chain-mainnet"
# The provider tag every Robinhood-universe row will carry. Binance rows carry
# BINANCE_SPOT; the two universes are never blended (spec §4).
ROBINHOOD_PROVIDER_ID = "ROBINHOOD_CHAIN"

# JSON-RPC error codes that mean "the request was malformed" — our fault, not
# the chain's. Everything else is treated as the source being unavailable.
_CALLER_FAULT_CODES = {-32700, -32600, -32601, -32602}


@dataclass(slots=True)
class BlockStamp:
    """A block's number and time, in this project's units (ms)."""

    number: int
    timestamp_ms: int
    hash: str | None = None


def _hex_to_int(value: object, *, what: str) -> int:
    """EVM quantities arrive as 0x-prefixed hex strings. Anything else is a
    payload-shape error worth naming, not coercing."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16)
        except ValueError:
            pass
    raise DataUnavailable(f"robinhood-rpc: {what} is not a hex quantity", detail={"value": repr(value)[:100]})


class RobinhoodRpc:
    """Minimal Ethereum JSON-RPC client for Robinhood Chain.

    Carries the shared limiter/breaker/retry machinery via HttpClient, so the
    rate-limited public endpoint is protected by the same budget discipline as
    the exchanges.
    """

    name = "ROBINHOOD-CHAIN-RPC"

    def __init__(self, settings: RobinhoodSettings, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.settings = settings
        self.clock = clock
        self._ids = count(1)
        self.http = HttpClient(
            settings.rpc_url,
            name=self.name,
            weight_per_minute=settings.rpc_weight_per_minute,
            max_concurrent=4,
            timeout=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
            retry_base_delay=settings.retry_base_delay_seconds,
            fallback_base_url=settings.rpc_fallback_url,
        )

    async def close(self) -> None:
        await self.http.close()

    @property
    def endpoint_host(self) -> str:
        """Host only — safe to log and display. The full URL may carry an API
        key path segment (Alchemy-style), which makes it a credential."""
        return self.settings.rpc_url.split("://", 1)[-1].split("/")[0]

    async def call(self, method: str, params: list | None = None) -> object:
        """One JSON-RPC call, envelope unwrapped, errors typed."""
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        raw = await self.http.post_json("/", payload)
        if not isinstance(raw, dict):
            raise SourceUnavailable(f"robinhood-rpc: non-object response to {method}")
        if "error" in raw and raw["error"]:
            err = raw["error"] if isinstance(raw["error"], dict) else {"message": str(raw["error"])}
            code = err.get("code")
            message = str(err.get("message", "unknown RPC error"))[:200]
            detail = {"method": method, "code": code}
            if code in _CALLER_FAULT_CODES:
                raise DataUnavailable(f"robinhood-rpc: {message}", detail=detail)
            raise SourceUnavailable(f"robinhood-rpc: {message}", detail=detail)
        if "result" not in raw:
            # An envelope with neither result nor error is not JSON-RPC at all —
            # likely a proxy or captive portal answering in its place.
            raise SourceUnavailable(f"robinhood-rpc: no result in response to {method}")
        return raw["result"]

    # ------------------------------------------------------------- methods --

    async def client_version(self) -> str:
        result = await self.call("web3_clientVersion")
        return str(result)

    async def chain_id(self) -> int:
        return _hex_to_int(await self.call("eth_chainId"), what="chainId")

    async def block_number(self) -> int:
        return _hex_to_int(await self.call("eth_blockNumber"), what="blockNumber")

    async def latest_block(self) -> BlockStamp:
        result = await self.call("eth_getBlockByNumber", ["latest", False])
        if not isinstance(result, dict):
            raise DataUnavailable("robinhood-rpc: latest block is not an object")
        return BlockStamp(
            number=_hex_to_int(result.get("number"), what="block.number"),
            timestamp_ms=_hex_to_int(result.get("timestamp"), what="block.timestamp") * 1000,
            hash=result.get("hash"),
        )

    async def get_logs(self, from_block: int, to_block: int) -> list:
        result = await self.call(
            "eth_getLogs",
            [{"fromBlock": hex(from_block), "toBlock": hex(to_block)}],
        )
        if not isinstance(result, list):
            raise DataUnavailable("robinhood-rpc: eth_getLogs did not return a list")
        return result

    async def get_code(self, address: str) -> str:
        result = await self.call("eth_getCode", [address, "latest"])
        return str(result)
