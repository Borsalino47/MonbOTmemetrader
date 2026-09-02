"""Ticker naming equivalences, in one place.

Venues and data sources disagree about names for the same asset: Kraken calls
bitcoin XBT, dogecoin XDG; MATIC was renamed POL and venues migrated on
different dates; RNDR became RENDER. Every one of those is a *rename*, not an
approximation — and every one of them, left unhandled, silently drops an asset
from a universe or attaches the wrong market cap to it.

Nothing in here is a fuzzy match. If two tickers are listed as equivalent it is
because they name the same asset.
"""

from __future__ import annotations

__all__ = ["VENUE_ALIASES", "CANONICAL_BASE", "canonical_base", "split_symbol"]

# base asset -> every ticker a venue might list it under, most-likely first.
VENUE_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("BTC", "XBT"),
    "DOGE": ("DOGE", "XDG"),
    "POL": ("POL", "MATIC"),
    "MATIC": ("MATIC", "POL"),
    "RENDER": ("RENDER", "RNDR"),
    "RNDR": ("RNDR", "RENDER"),
}

# venue ticker -> the name the rest of the world uses. Used when handing a
# symbol to a source that is not the venue (a valuation API, say).
CANONICAL_BASE: dict[str, str] = {
    "XBT": "BTC",
    "XDG": "DOGE",
    "RNDR": "RENDER",
    "MATIC": "POL",
}


def canonical_base(base: str) -> str:
    """The widely-used name for a venue-specific ticker. Unknown names pass through."""
    return CANONICAL_BASE.get(base.upper(), base.upper())


def split_symbol(symbol: str, quote_asset: str) -> str | None:
    """Base asset of a venue symbol, or None when it is not quoted in `quote_asset`.

    Suffix matching rather than a fixed length, because quote assets differ in
    length (USD, USDT, USDC) and a symbol is not always BASE+QUOTE of known sizes.
    """
    s, q = symbol.upper(), quote_asset.upper()
    if not q or not s.endswith(q) or len(s) <= len(q):
        return None
    return s[: -len(q)]
