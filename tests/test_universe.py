"""The Robinhood universe filter.

The failure this file exists to prevent: silently scanning the wrong set. Either
direction is expensive — scanning an asset you cannot buy wastes the rate-limit
budget and your attention, and dropping one you can buy hides the signal you
were waiting for. Both must be visible, never silent.
"""

from __future__ import annotations

import json

import pytest

from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import FrozenClock
from cryptopulse.providers.fixture import FixtureProvider
from cryptopulse.scanner.cex import CexScanner
from cryptopulse.universe.robinhood import (
    SNAPSHOT_BASES,
    SNAPSHOT_DATE,
    load_bases,
    resolve_universe,
)
from cryptopulse.universe.symbols import canonical_base, split_symbol
from tests.conftest import FIXED_NOW_MS

BINANCE_LIKE = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "SHIBUSDT", "PEPEUSDT", "NOTLISTEDUSDT"]
KRAKEN_LIKE = ["XBTUSDT", "ETHUSDT", "SOLUSDT", "XDGUSDT", "MATICUSDT"]


# --------------------------------------------------------------------------- #
# The snapshot itself
# --------------------------------------------------------------------------- #


def test_the_snapshot_is_unique_uppercase_and_dated():
    assert len(SNAPSHOT_BASES) == len(set(SNAPSHOT_BASES))
    assert all(b == b.upper() and b.isalnum() for b in SNAPSHOT_BASES)
    assert list(SNAPSHOT_BASES) == sorted(SNAPSHOT_BASES)
    assert SNAPSHOT_DATE.count("-") == 2  # a real date, so staleness is visible


def test_extra_and_exclude_are_applied_on_top_of_the_snapshot():
    bases, source, _ = load_bases(extra=["fartcoin"], exclude=["BTC"])
    assert "FARTCOIN" in bases
    assert "BTC" not in bases
    assert source == "snapshot"


def test_a_user_maintained_file_overrides_the_snapshot(tmp_path):
    path = tmp_path / "u.json"
    path.write_text(json.dumps({"as_of": "2026-01-02", "bases": ["btc", "wif"]}))
    bases, source, as_of = load_bases(file_path=str(path))
    assert bases == ["BTC", "WIF"]
    assert source.startswith("file:")
    assert as_of == "2026-01-02"


def test_a_broken_override_falls_back_loudly_instead_of_emptying_the_universe():
    """An unreadable file must not become "scan nothing"."""
    bases, source, _ = load_bases(file_path="/nonexistent/path/universe.json")
    assert bases == list(SNAPSHOT_BASES)
    assert "unreadable" in source


# --------------------------------------------------------------------------- #
# Resolution against a venue
# --------------------------------------------------------------------------- #


def test_resolution_maps_bases_onto_the_symbols_the_venue_actually_lists():
    res = resolve_universe(["BTC", "ETH", "PEPE"], BINANCE_LIKE, "USDT")
    assert res.symbols == ["BTCUSDT", "ETHUSDT", "PEPEUSDT"]
    assert res.by_base["BTC"] == "BTCUSDT"
    assert res.missing == []


def test_kraken_names_are_resolved_rather_than_guessed():
    """XBT is bitcoin and XDG is dogecoin. Constructing BTCUSDT here would scan nothing."""
    res = resolve_universe(["BTC", "DOGE", "POL"], KRAKEN_LIKE, "USDT")
    assert res.by_base["BTC"] == "XBTUSDT"
    assert res.by_base["DOGE"] == "XDGUSDT"
    # POL and MATIC are the same asset; the venue still lists the old name.
    assert res.by_base["POL"] == "MATICUSDT"


def test_an_asset_the_venue_does_not_carry_is_reported_not_dropped_silently():
    res = resolve_universe(["BTC", "TRUMP", "PENGU"], BINANCE_LIKE, "USDT")
    assert res.symbols == ["BTCUSDT"]
    assert res.missing == ["TRUMP", "PENGU"]
    assert any("not carried by this venue" in n for n in res.notes)


def test_every_resolution_carries_the_warning_that_prices_are_not_robinhoods():
    res = resolve_universe(["BTC"], BINANCE_LIKE, "USDT")
    joined = " ".join(res.notes)
    assert "NOT from Robinhood" in joined
    assert "hand-maintained" in joined


def test_two_aliases_of_one_asset_cannot_be_scanned_twice():
    res = resolve_universe(["POL", "MATIC"], ["MATICUSDT"], "USDT")
    assert res.symbols == ["MATICUSDT"]


# --------------------------------------------------------------------------- #
# Symbol helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("symbol", "quote", "expected"),
    [
        ("BTCUSDT", "USDT", "BTC"),
        ("XBTUSD", "USD", "XBT"),
        ("BTCUSDT", "USD", None),  # suffix must match exactly
        ("USDT", "USDT", None),  # a symbol that is only its quote has no base
    ],
)
def test_split_symbol_matches_on_the_quote_suffix(symbol, quote, expected):
    assert split_symbol(symbol, quote) == expected


def test_canonical_base_translates_venue_tickers_for_outside_sources():
    assert canonical_base("XBT") == "BTC"
    assert canonical_base("xdg") == "DOGE"
    assert canonical_base("SOL") == "SOL"  # unknown names pass through unchanged


# --------------------------------------------------------------------------- #
# End to end through the scanner
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_scanner_scans_only_the_robinhood_universe():
    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.scanner.universe = "robinhood"
    settings.database.url = "sqlite:///:memory:"

    clock = FrozenClock(FIXED_NOW_MS)
    scanner = CexScanner(settings, provider=FixtureProvider(clock=clock), clock=clock)
    report = await scanner.scan()

    scanned = {r.symbol for r in report.results}
    assert "BTCUSDT" in scanned
    # BNB and JUP are in the synthetic venue but not on Robinhood.
    assert "BNBUSDT" not in scanned
    assert "JUPUSDT" not in scanned
    # The illiquid trap asset is not a Robinhood listing either.
    assert "MICROUSDT" not in scanned
    # Aliases resolved against what the venue actually lists.
    assert "MATICUSDT" in scanned and "RNDRUSDT" in scanned

    assert scanner.universe_resolution is not None
    assert scanner.universe_resolution.by_base["POL"] == "MATICUSDT"
    assert any("Robinhood" in n for n in report.notes)
    await scanner.close()


@pytest.mark.asyncio
async def test_the_volume_universe_is_unchanged_by_the_robinhood_work():
    """The V1 behaviour has to remain available and identical."""
    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.scanner.universe = "volume"
    settings.database.url = "sqlite:///:memory:"

    clock = FrozenClock(FIXED_NOW_MS)
    scanner = CexScanner(settings, provider=FixtureProvider(clock=clock), clock=clock)
    report = await scanner.scan()

    scanned = {r.symbol for r in report.results}
    assert "BNBUSDT" in scanned
    assert scanner.universe_resolution is None
    await scanner.close()
