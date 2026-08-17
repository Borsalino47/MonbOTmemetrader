"""DexScreener — the second, independent view of a token's market.

WHY A SECOND PRICE SOURCE

`doctor` has always cross-checked Binance's ticker against its own klines,
because a well-formed payload with the fields in the wrong order is exactly the
failure a scanner cannot detect from the inside. The same reasoning applies to
an on-chain token: GeckoTerminal and DexScreener index the same pools from the
same chain, so when they disagree about a price, one of them is wrong and the
number should not be acted on.

They are not redundant, either. GeckoTerminal returns unique buyers and sellers;
DexScreener returns per-pair liquidity split into base and quote sides and the
pair's own creation time. Each fills gaps in the other.

**Disagreement is surfaced, never averaged.** A mean of two prices that differ by
40 % describes neither, and would hide the one fact worth knowing — that
something is wrong with the data for this token.

THE CONTRACT

Cross-checked against `dexscreener` 1.3 from PyPI: base `https://api.dexscreener.com`,
`/latest/dex/tokens/{address}` returning `{pairs: [...]}`, 300 requests/minute on
that endpoint, and the field names below. The host is refused by this
environment, like every other; `doctor-robinhood` is what proves it live.
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.errors import SourceUnavailable
from cryptopulse.core.logging import get_logger
from cryptopulse.providers.http import HttpClient

log = get_logger("providers.dexscreener")

__all__ = ["PairSnapshot", "DexScreenerClient", "parse_pair"]


def _opt_float(value: object) -> float | None:
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


def _window(block: object, key: str) -> object:
    return block.get(key) if isinstance(block, dict) else None


@dataclass(slots=True)
class PairSnapshot:
    """One pair as DexScreener sees it. Every market figure optional."""

    pair_address: str
    chain_slug: str | None
    dex_id: str | None
    url: str | None
    base_address: str | None
    base_symbol: str | None
    quote_address: str | None
    quote_symbol: str | None
    price_usd: float | None = None
    price_native: float | None = None
    liquidity_usd: float | None = None
    liquidity_base: float | None = None
    liquidity_quote: float | None = None
    fdv_usd: float | None = None
    market_cap_usd: float | None = None
    created_at_ms: int | None = None
    volume_usd: dict[str, float] | None = None
    price_change_pct: dict[str, float] | None = None
    buys: dict[str, int] | None = None
    sells: dict[str, int] | None = None

    def __post_init__(self) -> None:
        for name in ("volume_usd", "price_change_pct", "buys", "sells"):
            if getattr(self, name) is None:
                setattr(self, name, {})

    def to_dict(self) -> dict:
        return {
            "source": "DEXSCREENER",
            "pair_address": self.pair_address,
            "dex": self.dex_id,
            "url": self.url,
            "base_symbol": self.base_symbol,
            "quote_symbol": self.quote_symbol,
            "price_usd": self.price_usd,
            "liquidity_usd": self.liquidity_usd,
            "fdv_usd": self.fdv_usd,
            "market_cap_usd": self.market_cap_usd,
            "created_at_ms": self.created_at_ms,
            "volume_usd": self.volume_usd,
            "price_change_pct": self.price_change_pct,
            "buys": self.buys,
            "sells": self.sells,
        }


_INTERVALS = ("m5", "h1", "h6", "h24")


def parse_pair(raw: object) -> PairSnapshot | None:
    """One `pairs[]` element, or None if it is not a usable pair."""
    if not isinstance(raw, dict):
        return None
    address = raw.get("pairAddress")
    if not isinstance(address, str) or not address:
        return None

    base = raw.get("baseToken") if isinstance(raw.get("baseToken"), dict) else {}
    quote = raw.get("quoteToken") if isinstance(raw.get("quoteToken"), dict) else {}
    liquidity = raw.get("liquidity") if isinstance(raw.get("liquidity"), dict) else {}

    def numeric_map(block: object) -> dict[str, float]:
        out: dict[str, float] = {}
        if isinstance(block, dict):
            for key in _INTERVALS:
                value = _opt_float(block.get(key))
                if value is not None:
                    out[key] = value
        return out

    txns = raw.get("txns")

    def tx_map(field: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for key in _INTERVALS:
            window = _window(txns, key)
            if isinstance(window, dict):
                value = _opt_int(window.get(field))
                if value is not None:
                    out[key] = value
        return out

    return PairSnapshot(
        pair_address=address,
        chain_slug=raw.get("chainId") if isinstance(raw.get("chainId"), str) else None,
        dex_id=raw.get("dexId") if isinstance(raw.get("dexId"), str) else None,
        url=raw.get("url") if isinstance(raw.get("url"), str) else None,
        base_address=base.get("address") if isinstance(base.get("address"), str) else None,
        base_symbol=base.get("symbol") if isinstance(base.get("symbol"), str) else None,
        quote_address=quote.get("address") if isinstance(quote.get("address"), str) else None,
        quote_symbol=quote.get("symbol") if isinstance(quote.get("symbol"), str) else None,
        price_usd=_opt_float(raw.get("priceUsd")),
        price_native=_opt_float(raw.get("priceNative")),
        liquidity_usd=_opt_float(liquidity.get("usd")),
        liquidity_base=_opt_float(liquidity.get("base")),
        liquidity_quote=_opt_float(liquidity.get("quote")),
        fdv_usd=_opt_float(raw.get("fdv")),
        market_cap_usd=_opt_float(raw.get("marketCap")),
        # Already milliseconds, unlike GeckoTerminal's ISO seconds. Converting
        # one and not the other is exactly how two "ages" end up 1000x apart.
        created_at_ms=_opt_int(raw.get("pairCreatedAt")),
        volume_usd=numeric_map(raw.get("volume")),
        price_change_pct=numeric_map(raw.get("priceChange")),
        buys=tx_map("buys"),
        sells=tx_map("sells"),
    )


class DexScreenerClient:
    """Read-only client for DexScreener pairs."""

    name = "DEXSCREENER"

    def __init__(self, settings: RobinhoodSettings, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self.settings = settings
        self.clock = clock
        self.http = HttpClient(
            settings.dexscreener_base_url,
            name=self.name,
            weight_per_minute=settings.dexscreener_calls_per_minute,
            max_concurrent=4,
            timeout=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
            retry_base_delay=settings.retry_base_delay_seconds,
            headers={"accept": "application/json"},
        )

    async def close(self) -> None:
        await self.http.close()

    @property
    def chain_slug(self) -> str:
        return self.settings.dexscreener_chain

    async def token_pairs(self, address: str) -> list[PairSnapshot]:
        """Every pair DexScreener knows for this token, on this chain only.

        The endpoint answers across all chains, so the slug filter is not
        cosmetic: a token whose address also exists on another chain would
        otherwise contribute that chain's liquidity to this one's total.
        """
        raw = await self.http.get_json(f"/latest/dex/tokens/{address}")
        if not isinstance(raw, dict):
            raise SourceUnavailable(f"{self.name}: non-object response")
        pairs = raw.get("pairs")
        if pairs is None:
            # A token nobody has indexed yet returns null rather than an error.
            # Absent is absent; it is not an outage and not an empty market.
            return []
        if not isinstance(pairs, list):
            raise SourceUnavailable(f"{self.name}: pairs is not a list")

        out: list[PairSnapshot] = []
        for entry in pairs:
            pair = parse_pair(entry)
            if pair is None:
                continue
            if pair.chain_slug and pair.chain_slug.lower() != self.chain_slug.lower():
                continue
            out.append(pair)
        return out
