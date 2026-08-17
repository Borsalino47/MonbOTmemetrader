"""GeckoTerminal — the DEX indexer that answers "which pools are new".

WHY THIS EXISTS AT ALL

`providers/robinhood.py` can prove the chain is alive; it cannot say which
tokens started trading in the last quarter of an hour. That needs pool
creations and swaps already decoded, which is an indexing job, not an RPC one.
GeckoTerminal indexes Robinhood Chain under the network id `robinhood`, is free,
and publishes a 30 calls/minute limit.

WHAT THE CONTRACT IS, AND HOW IT WAS ESTABLISHED

The host is unreachable from the environment this was written in (the same 403
allowlist refusal as Binance and the RPC). The contract below therefore comes
from three independent readings that agree:

  * `geckoterminal-api` 0.9.0, downloaded from PyPI and read — it pins the base
    URL `https://api.geckoterminal.com/api/v2`, the endpoint
    `/networks/{network}/new_pools`, the `include` parameter and the 10-page cap;
  * GeckoTerminal's own published documentation and changelogs, for the
    attribute names;
  * a third-party schema reference, for the nesting of `volume_usd`,
    `price_change_percentage` and `transactions`.

That is a second and third opinion, not a verification. `cryptopulse
doctor-robinhood` on a machine with egress is what turns this into a fact.

TWO PARSING RULES THAT CARRY THE WHOLE MODULE

1. **Every number arrives as a string, and may be null.** `"reserve_in_usd":
   "14348.4777"`. A field that is absent, null or unparseable becomes `None` —
   never 0.0. Zero liquidity and unknown liquidity are opposite facts, and the
   safety gate treats them as such.

2. **The new token is not always the base token.** A pool is TOKEN/WETH or
   WETH/TOKEN depending on address ordering, and taking `base_token` blindly
   would price half the discoveries as if WETH were the new asset. The side is
   decided by which one is a known quote asset, and the price follows the side.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.errors import DataUnavailable, SourceUnavailable
from cryptopulse.core.logging import get_logger
from cryptopulse.providers.http import HttpClient

log = get_logger("providers.geckoterminal")

__all__ = ["QUOTE_ASSETS", "PoolSnapshot", "TokenRef", "GeckoTerminalClient"]

# Assets that are the *quote* side of a pair, never the discovery. Lower-cased
# symbols, because this is the one place a symbol is the only signal available:
# the relationship gives an address, and hardcoding Robinhood Chain's WETH
# address is precisely the kind of unverified constant this project refuses.
QUOTE_ASSETS = frozenset({"weth", "eth", "usdc", "usdt", "dai", "usds", "usd1", "wsteth"})


def _opt_float(value: object) -> float | None:
    """A number, or None. Never a zero standing in for a missing field."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _opt_int(value: object) -> int | None:
    f = _opt_float(value)
    return None if f is None else int(f)


def _parse_iso_ms(value: object) -> int | None:
    """`"2023-12-23T22:19:36Z"` -> epoch ms. Unparseable is None, not now()."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        # fromisoformat handles the trailing Z only from 3.11; normalise anyway
        # so the parser does not depend on the interpreter's minor version.
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _interval(block: object, key: str) -> dict:
    """One of the m5/h1/h6/h24 sub-objects, or an empty dict."""
    if isinstance(block, dict):
        inner = block.get(key)
        if isinstance(inner, dict):
            return inner
    return {}


@dataclass(slots=True)
class TokenRef:
    """A token as the relationships block names it: `robinhood_0xabc…`."""

    address: str | None
    symbol: str | None = None
    name: str | None = None

    @property
    def is_quote_asset(self) -> bool:
        return (self.symbol or "").strip().lower() in QUOTE_ASSETS


@dataclass(slots=True)
class PoolSnapshot:
    """One pool as the indexer currently sees it. Every market number optional.

    This is a reading, not a judgement: no score, no threshold, no derived
    opinion lives here. Everything is either what the API said or None.
    """

    pool_address: str
    name: str | None
    dex: str | None
    base: TokenRef
    quote: TokenRef
    created_at_ms: int | None

    base_price_usd: float | None = None
    quote_price_usd: float | None = None
    liquidity_usd: float | None = None
    fdv_usd: float | None = None
    market_cap_usd: float | None = None

    # Keyed by interval name ("m5", "h1", "h6", "h24"). A missing interval is an
    # absent key, so `.get()` yields None rather than a fabricated zero.
    volume_usd: dict[str, float] = None  # type: ignore[assignment]
    price_change_pct: dict[str, float] = None  # type: ignore[assignment]
    buys: dict[str, int] = None  # type: ignore[assignment]
    sells: dict[str, int] = None  # type: ignore[assignment]
    buyers: dict[str, int] = None  # type: ignore[assignment]
    sellers: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        for field in ("volume_usd", "price_change_pct", "buys", "sells", "buyers", "sellers"):
            if getattr(self, field) is None:
                setattr(self, field, {})

    # -- which side is the discovery ---------------------------------------- #

    @property
    def token(self) -> TokenRef:
        """The token this pool is *about*.

        Address ordering decides base vs quote in an AMM, so a new token is the
        base side roughly half the time. Reading `base_token` blindly would
        report WETH as the discovery on the other half, at its own price.
        """
        if self.base.is_quote_asset and not self.quote.is_quote_asset:
            return self.quote
        return self.base

    @property
    def is_discovery(self) -> bool:
        """Is there a new token here at all?

        Found by running the search: a WETH/USDC pool has no new side, and
        `token` had to return *something*, so it returned WETH — presenting a
        blue chip as a fresh discovery, priced correctly and completely wrong.
        A pool between two known quote assets is infrastructure, not a find.
        """
        return not (self.base.is_quote_asset and self.quote.is_quote_asset)

    @property
    def token_price_usd(self) -> float | None:
        """The price of `token`, taken from the matching side."""
        return self.quote_price_usd if self.token is self.quote else self.base_price_usd

    def age_seconds(self, *, now_ms: int) -> float | None:
        if self.created_at_ms is None:
            return None
        return max(0.0, (now_ms - self.created_at_ms) / 1000.0)

    def to_dict(self) -> dict:
        return {
            "pool_address": self.pool_address,
            "name": self.name,
            "dex": self.dex,
            "created_at_ms": self.created_at_ms,
            "token_address": self.token.address,
            "token_symbol": self.token.symbol,
            "token_name": self.token.name,
            "price_usd": self.token_price_usd,
            "liquidity_usd": self.liquidity_usd,
            "fdv_usd": self.fdv_usd,
            "market_cap_usd": self.market_cap_usd,
            "volume_usd": self.volume_usd,
            "price_change_pct": self.price_change_pct,
            "buys": self.buys,
            "sells": self.sells,
            "buyers": self.buyers,
            "sellers": self.sellers,
        }


def _token_ref(included: dict, ref: object) -> TokenRef:
    """Resolve a relationship pointer against the `included` block."""
    if not isinstance(ref, dict):
        return TokenRef(address=None)
    data = ref.get("data")
    if not isinstance(data, dict):
        return TokenRef(address=None)
    key = data.get("id")
    entry = included.get(key) if isinstance(key, str) else None
    attrs = entry.get("attributes", {}) if isinstance(entry, dict) else {}
    # The id is `{network}_{address}`; the address after the first underscore is
    # the fallback when the token was not included in the response.
    address = attrs.get("address")
    if not address and isinstance(key, str) and "_" in key:
        address = key.split("_", 1)[1]
    return TokenRef(
        address=address if isinstance(address, str) else None,
        symbol=attrs.get("symbol") if isinstance(attrs.get("symbol"), str) else None,
        name=attrs.get("name") if isinstance(attrs.get("name"), str) else None,
    )


def parse_pool(entry: dict, included: dict) -> PoolSnapshot | None:
    """One `data[]` element into a snapshot, or None if it is not a pool.

    Returning None rather than raising: one malformed row must not lose the
    other thirty-nine, exactly as one failing symbol never stops a scan.
    """
    if not isinstance(entry, dict):
        return None
    attrs = entry.get("attributes")
    if not isinstance(attrs, dict):
        return None
    address = attrs.get("address")
    if not isinstance(address, str) or not address:
        return None

    rel = entry.get("relationships", {})
    rel = rel if isinstance(rel, dict) else {}
    dex_ref = rel.get("dex", {})
    dex_id = None
    if isinstance(dex_ref, dict) and isinstance(dex_ref.get("data"), dict):
        dex_id = dex_ref["data"].get("id")

    tx = attrs.get("transactions")
    intervals = ("m5", "h1", "h6", "h24")

    def numeric_map(block: object) -> dict[str, float]:
        if not isinstance(block, dict):
            return {}
        out: dict[str, float] = {}
        for key in intervals:
            value = _opt_float(block.get(key))
            if value is not None:
                out[key] = value
        return out

    def tx_map(field: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for key in intervals:
            value = _opt_int(_interval(tx, key).get(field))
            if value is not None:
                out[key] = value
        return out

    return PoolSnapshot(
        pool_address=address,
        name=attrs.get("name") if isinstance(attrs.get("name"), str) else None,
        dex=dex_id if isinstance(dex_id, str) else None,
        base=_token_ref(included, rel.get("base_token")),
        quote=_token_ref(included, rel.get("quote_token")),
        created_at_ms=_parse_iso_ms(attrs.get("pool_created_at")),
        base_price_usd=_opt_float(attrs.get("base_token_price_usd")),
        quote_price_usd=_opt_float(attrs.get("quote_token_price_usd")),
        liquidity_usd=_opt_float(attrs.get("reserve_in_usd")),
        fdv_usd=_opt_float(attrs.get("fdv_usd")),
        market_cap_usd=_opt_float(attrs.get("market_cap_usd")),
        volume_usd=numeric_map(attrs.get("volume_usd")),
        price_change_pct=numeric_map(attrs.get("price_change_percentage")),
        buys=tx_map("buys"),
        sells=tx_map("sells"),
        buyers=tx_map("buyers"),
        sellers=tx_map("sellers"),
    )


class GeckoTerminalClient:
    """Read-only client for the pools GeckoTerminal has indexed."""

    name = "GECKOTERMINAL"

    def __init__(self, settings: RobinhoodSettings, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.settings = settings
        self.clock = clock
        self.http = HttpClient(
            settings.geckoterminal_base_url,
            name=self.name,
            weight_per_minute=settings.geckoterminal_calls_per_minute,
            max_concurrent=2,
            timeout=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
            retry_base_delay=settings.retry_base_delay_seconds,
            headers={"accept": "application/json"},
        )

    async def close(self) -> None:
        await self.http.close()

    @property
    def network(self) -> str:
        return self.settings.geckoterminal_network

    async def _pools(self, path: str, page: int) -> list[PoolSnapshot]:
        raw = await self.http.get_json(
            path,
            params={"include": "base_token,quote_token,dex", "page": page},
        )
        if not isinstance(raw, dict):
            raise SourceUnavailable(f"{self.name}: non-object response for {path}")
        if "data" not in raw:
            # JSON:API errors come back under `errors`; surfacing the message is
            # the difference between "this network is unknown" and "we are down".
            errors = raw.get("errors")
            message = "no data in response"
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                message = str(errors[0].get("title") or errors[0].get("detail") or message)[:200]
            raise DataUnavailable(f"{self.name}: {message}", detail={"path": path})

        data = raw.get("data")
        if not isinstance(data, list):
            raise DataUnavailable(f"{self.name}: data is not a list", detail={"path": path})

        included: dict[str, dict] = {}
        for item in raw.get("included", []) or []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                included[item["id"]] = item

        pools: list[PoolSnapshot] = []
        skipped = 0
        for entry in data:
            pool = parse_pool(entry, included)
            if pool is None:
                skipped += 1
                continue
            pools.append(pool)
        if skipped:
            log.warning("geckoterminal_rows_skipped", path=path, skipped=skipped, kept=len(pools))
        return pools

    async def new_pools(self, page: int = 1) -> list[PoolSnapshot]:
        """The most recently created pools on this network, newest first."""
        return await self._pools(f"/networks/{self.network}/new_pools", page)

    async def trending_pools(self, page: int = 1) -> list[PoolSnapshot]:
        return await self._pools(f"/networks/{self.network}/trending_pools", page)

    async def token_pools(self, token_address: str, page: int = 1) -> list[PoolSnapshot]:
        """Every pool this indexer knows for one token.

        This is how a *held* token is re-read: `new_pools` only ever returns
        recently created ones, so a position opened two days ago has long since
        fallen out of the only listing the discovery search looks at.

        Deliberately one request per token. The reference client exposes
        `/pools/multi/{addresses}`, which takes *pool* addresses rather than
        token addresses, and no token->pools batch endpoint exists — claiming
        one request for ten tokens would be a cost this connector cannot
        actually deliver.
        """
        return await self._pools(
            f"/networks/{self.network}/tokens/{token_address}/pools", page
        )

    async def pool_ohlcv(
        self,
        pool_address: str,
        *,
        timeframe: str = "minute",
        aggregate: int = 5,
        before_timestamp: int | None = None,
        limit: int = 200,
    ) -> list[tuple[int, float, float, float, float, float]]:
        """Candles for one pool, oldest first, as (open_time_ms, o, h, l, c, v).

        This is what makes a Robinhood claim checkable. Without it, "what did
        the price do fifteen minutes after that decision?" could only be
        answered by looking at exactly the right moment and recording whatever
        was on screen — and a window observed three minutes late is not the
        window the engine made a claim about.

        Contract read from `geckoterminal-api` 0.9.0: timeframe is one of
        day / hour / minute, aggregate is 1 for day, 1|4|12 for hour and
        1|5|15 for minute, and the payload is a list of
        `[timestamp_seconds, open, high, low, close, volume]`. The host is
        refused by this build environment, so this is a second opinion about
        the shape, not a verification of the live API.
        """
        if timeframe not in ("day", "hour", "minute"):
            raise ValueError(f"timeframe {timeframe!r} inconnu (day / hour / minute)")
        params: dict = {
            "aggregate": aggregate,
            "limit": min(int(limit), 1000),
            "currency": "usd",
            # The token whose price the candles describe. `base` is the pool's
            # base side, which is not necessarily the token we care about —
            # callers that need the quote side pass it through the pool.
            "token": "base",
        }
        if before_timestamp is not None:
            params["before_timestamp"] = int(before_timestamp)

        raw = await self.http.get_json(
            f"/networks/{self.network}/pools/{pool_address}/ohlcv/{timeframe}",
            params=params,
        )
        if not isinstance(raw, dict):
            raise SourceUnavailable(f"{self.name}: non-object OHLCV response")
        attributes = ((raw.get("data") or {}).get("attributes") or {})
        rows = attributes.get("ohlcv_list")
        if not isinstance(rows, list):
            raise DataUnavailable(
                f"{self.name}: ohlcv_list absent", detail={"pool": pool_address}
            )

        out: list[tuple[int, float, float, float, float, float]] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                ts, o, h, low, c, v = row[:6]
                out.append(
                    (int(ts) * 1000, float(o), float(h), float(low), float(c), float(v))
                )
            except (TypeError, ValueError):
                # A malformed row is skipped rather than zero-filled: a candle
                # that could not be parsed is not a candle at price zero.
                continue
        # The API returns newest first; every consumer here reads chronologically.
        out.sort(key=lambda r: r[0])
        return out

    async def list_networks(self) -> list[str]:
        """Network ids the indexer knows. Used by the doctor to prove that
        `robinhood` is one of them rather than assuming it."""
        raw = await self.http.get_json("/networks", params={"page": 1})
        if not isinstance(raw, dict) or not isinstance(raw.get("data"), list):
            raise SourceUnavailable(f"{self.name}: unexpected /networks response")
        return [
            item["id"] for item in raw["data"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
