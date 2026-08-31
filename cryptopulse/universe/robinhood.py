"""The Robinhood Crypto universe: scan only what you can actually buy.

--------------------------------------------------------------------------
WHY THIS EXISTS

A radar that surfaces a coin you cannot trade wastes the only thing it is
supposed to save you — the minutes between a signal and a decision. Robinhood
Crypto lists a few dozen assets out of the ten thousand that exist, so the
universe filter is not a nicety here, it is most of the product.

WHERE THE PRICES COME FROM (READ THIS)

Robinhood publishes **no public market-data API**. Its Crypto Trading API needs
an API key and an Ed25519 request signature, and it serves quotes for your own
account, not a scannable historical feed. So this module does NOT fetch prices
from Robinhood. It decides *which symbols to scan*; the candles still come from
the configured venue (Binance or Kraken).

That split has a consequence you must understand before trusting a number on
screen: **the price you see is the reference venue's price, not Robinhood's.**
Robinhood's spread and its execution price will differ, sometimes materially on
a thin asset during a fast move. This scanner ranks behaviour; it does not quote
you a fill.

--------------------------------------------------------------------------
VERIFICATION STATUS: the listing below is a **hand-maintained snapshot**, NOT
LIVE VERIFIED. It was written from public knowledge of what Robinhood Crypto
lists, in an environment with no access to Robinhood, and Robinhood adds and
removes assets without notice.

Both ways it can be wrong are safe and visible:

* a symbol listed here that the venue does not carry is reported in
  `UniverseResolution.missing` and skipped — it cannot produce a bad signal;
* an asset Robinhood lists that is missing here is simply never scanned.

Neither silently corrupts a score. To correct it, either set
`CP_SCAN_ROBINHOOD_EXTRA` / `CP_SCAN_ROBINHOOD_EXCLUDE`, or point
`CP_SCAN_ROBINHOOD_FILE` at a JSON file of base assets you maintain yourself —
which is also what `cryptopulse universe --refresh` writes.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from cryptopulse.core.logging import get_logger
from cryptopulse.universe.symbols import VENUE_ALIASES

log = get_logger("universe.robinhood")

__all__ = [
    "SNAPSHOT_DATE",
    "SNAPSHOT_BASES",
    "VENUE_ALIASES",
    "UniverseResolution",
    "load_bases",
    "resolve_universe",
    "fetch_live_catalog",
]

# Date this list was last edited by hand. If it is far in the past, treat every
# absence as "probably stale" rather than "not listed".
SNAPSHOT_DATE = "2026-08-31"

# Base assets believed tradable on Robinhood Crypto (US). Alphabetical.
# Keep additions alphabetical and never add a symbol you have not seen listed —
# a wrong entry costs a scan slot and shows up as a permanent `missing` row.
SNAPSHOT_BASES: tuple[str, ...] = (
    "AAVE",
    "ADA",
    "ARB",
    "AVAX",
    "BCH",
    "BONK",
    "BTC",
    "COMP",
    "CRV",
    "DOGE",
    "DOT",
    "ETC",
    "ETH",
    "GRT",
    "HBAR",
    "LINK",
    "LTC",
    "MKR",
    "NEAR",
    "ONDO",
    "PENGU",
    "PEPE",
    "POL",  # ex-MATIC; both names are resolved, see VENUE_ALIASES
    "RENDER",  # ex-RNDR
    "SHIB",
    "SOL",
    "SUI",
    "TRUMP",
    "UNI",
    "WIF",
    "XLM",
    "XRP",
    "XTZ",
    "YFI",
)

# Naming equivalences (XBT for BTC on Kraken, POL for MATIC after the rename)
# live in universe/symbols.py, because the valuation source needs them too.
# Resolution tries each alias against the symbols the venue actually returned,
# so a rename in either direction is absorbed without guessing.


@dataclass(slots=True)
class UniverseResolution:
    """What the venue could and could not supply for the requested universe."""

    symbols: list[str]  # venue symbols, e.g. BTCUSDT / XBTUSD
    by_base: dict[str, str] = field(default_factory=dict)  # DOGE -> XDGUSDT
    missing: list[str] = field(default_factory=list)  # bases the venue does not list
    source: str = "snapshot"
    as_of: str = SNAPSHOT_DATE
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "count": len(self.symbols),
            "symbols": self.symbols,
            "by_base": self.by_base,
            "missing": self.missing,
            "source": self.source,
            "as_of": self.as_of,
            "notes": self.notes,
        }


def load_bases(
    *,
    file_path: str | None = None,
    extra: list[str] | None = None,
    exclude: list[str] | None = None,
) -> tuple[list[str], str, str]:
    """The base assets to scan, and where the list came from.

    Precedence: a user-maintained JSON file wins over the built-in snapshot,
    because the user is the one who can see their own Robinhood app. `extra` and
    `exclude` are applied on top of whichever won.

    Returns (bases, source, as_of).
    """
    bases: list[str] = list(SNAPSHOT_BASES)
    source = "snapshot"
    as_of = SNAPSHOT_DATE

    if file_path:
        path = Path(file_path)
        try:
            payload = json.loads(path.read_text())
            loaded = payload.get("bases") if isinstance(payload, dict) else payload
            if not isinstance(loaded, list) or not loaded:
                raise ValueError("expected a non-empty list of base assets")
            bases = [str(b).strip().upper() for b in loaded if str(b).strip()]
            source = f"file:{path}"
            if isinstance(payload, dict):
                as_of = str(payload.get("as_of", "unknown"))
            else:
                as_of = "unknown"
        except (OSError, ValueError, TypeError) as exc:
            # A broken override must not silently become an empty universe.
            log.warning("robinhood_file_unreadable", path=str(file_path), error=str(exc)[:160])
            source = f"snapshot (file:{path} unreadable: {type(exc).__name__})"

    for b in extra or []:
        b = b.strip().upper()
        if b and b not in bases:
            bases.append(b)
    if exclude:
        drop = {b.strip().upper() for b in exclude}
        bases = [b for b in bases if b not in drop]

    return bases, source, as_of


def resolve_universe(
    bases: list[str],
    available: list[str] | set[str],
    quote_asset: str,
    *,
    source: str = "snapshot",
    as_of: str = SNAPSHOT_DATE,
) -> UniverseResolution:
    """Map base assets onto the symbols the venue actually returned.

    Resolution is done against the venue's own symbol list rather than by string
    construction, so a venue that names bitcoin XBT, or that has migrated MATIC
    to POL, resolves correctly instead of producing a symbol nobody carries.
    """
    quote = quote_asset.upper()
    have = {s.upper() for s in available}
    symbols: list[str] = []
    by_base: dict[str, str] = {}
    missing: list[str] = []

    for base in bases:
        base = base.upper()
        found = None
        for alias in VENUE_ALIASES.get(base, (base,)):
            candidate = f"{alias}{quote}"
            if candidate in have:
                found = candidate
                break
        if found is None:
            missing.append(base)
            continue
        if found not in symbols:  # two aliases of one asset must not scan twice
            symbols.append(found)
            by_base[base] = found

    notes = [
        "Universe restricted to assets believed tradable on Robinhood Crypto "
        f"({source}, as of {as_of}). This listing is hand-maintained and NOT verified against Robinhood.",
        "Prices and candles come from the configured venue, NOT from Robinhood. "
        "Robinhood's spread and fill will differ.",
    ]
    if missing:
        notes.append(
            f"{len(missing)} listed asset(s) not carried by this venue against {quote}: " + ", ".join(missing)
        )
    return UniverseResolution(
        symbols=symbols, by_base=by_base, missing=missing, source=source, as_of=as_of, notes=notes
    )


# --------------------------------------------------------------------------- #
# Optional live refresh
# --------------------------------------------------------------------------- #

# Robinhood's currency-pair catalogue. Historically served unauthenticated; that
# is NOT guaranteed and has never been verified from this project. Treat a
# failure here as normal, not as a fault.
CATALOG_URL = "https://api.robinhood.com/currency_pairs/"


async def fetch_live_catalog(timeout: float = 15.0) -> tuple[list[str], str]:
    """Try to read the live Robinhood currency-pair catalogue.

    Returns (bases, note). Raises nothing that a caller has to handle specially:
    on any failure it returns the empty list plus a note saying what happened, so
    the caller falls back to the snapshot rather than to an empty universe.

    NOT LIVE VERIFIED — this call has never succeeded from this project, because
    the environment it was written in cannot reach any exchange host.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(CATALOG_URL, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            return [], f"HTTP {resp.status_code} from {CATALOG_URL}"
        payload = resp.json()
    except Exception as exc:  # network, TLS, JSON — all mean "no catalogue"
        return [], f"{type(exc).__name__}: {str(exc)[:160]}"

    rows = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return [], "unexpected payload shape: no results list"

    bases: list[str] = []
    for row in rows:
        try:
            if row.get("display_only") is True:
                continue
            # Robinhood marks a pair's tradability explicitly; anything other
            # than tradable is not something a radar should surface.
            if str(row.get("tradability", "tradable")).lower() != "tradable":
                continue
            code = str(row["asset_currency"]["code"]).upper()
            if code and code not in bases:
                bases.append(code)
        except (AttributeError, KeyError, TypeError):
            continue

    if not bases:
        return [], "catalogue parsed but contained no tradable pairs"
    return sorted(bases), f"{len(bases)} tradable base assets from {CATALOG_URL}"
