"""The multi-market surface: two universes, one API, never blended.

What these tests pin (spec §§3-4, 6, 41-42, 47):

  * the provider list carries both markets with their own, independent states;
  * asking for Robinhood status never blocks and never runs at boot — the
    universe is lazy until someone looks at it;
  * a verified Binance feed never makes Robinhood look verified, and the
    Robinhood verdict never reads Binance state at all;
  * with no market-data phases implemented yet, the API says NON DISPONIBLE
    honestly instead of serving empty lists that would read as a quiet market.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cryptopulse.api.app import create_app
from cryptopulse.api.service import ScannerService, set_service
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.database.session import reset_engine
from cryptopulse.providers.robinhood_verify import ChainState, ChainVerification
from cryptopulse.providers.verify import FeedState

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def service(tmp_path):
    reset_engine()
    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.database.url = f"sqlite:///{tmp_path}/test.db"
    svc = ScannerService(settings)
    set_service(svc)
    yield svc
    set_service(None)
    reset_engine()


@pytest.fixture()
def client(service):
    app = create_app(start_loop=False)
    with TestClient(app) as c:
        yield c


def test_providers_lists_both_markets_with_independent_states(client):
    body = client.get("/api/providers").json()
    ids = [p["id"] for p in body["providers"]]
    assert ids == ["BINANCE_SPOT", "ROBINHOOD_CHAIN"]
    binance, robinhood = body["providers"]
    assert binance["label"] == "BINANCE" and binance["emoji"] == "🟡"
    assert robinhood["label"] == "ROBINHOOD" and robinhood["emoji"] == "🟣"
    assert robinhood["chain_id"] == 4663
    # Not yet checked is PENDING, never a copy of the Binance state.
    assert robinhood["state"] == "PENDING"
    assert robinhood["live_verified"] is False
    assert robinhood["market_data_available"] is False


def test_a_verified_binance_feed_does_not_verify_robinhood(client, service):
    """Spec §6, the sentence in bold: never declare Robinhood LIVE because
    Binance works."""
    service.feed.state = FeedState.VERIFIED
    body = client.get("/api/providers").json()
    binance, robinhood = body["providers"]
    assert binance["live_verified"] is True
    assert robinhood["live_verified"] is False
    assert robinhood["state"] == "PENDING"


def test_robinhood_status_answers_instantly_with_pending(client):
    """The switch must feel instant (spec §3): the first status call returns
    PENDING and kicks the doctor in the background rather than waiting on the
    network inside the request."""
    body = client.get("/api/provider/robinhood/status").json()
    assert body["provider"] == "ROBINHOOD_CHAIN"
    assert body["network"] == "robinhood-chain-mainnet"
    assert body["chain_id"] == 4663
    assert body["verification"]["state"] in ("PENDING", "FAILED")
    assert body["live_verified"] is False


def test_market_data_is_declared_unavailable_not_faked(client):
    """Spec §56: nothing is presented as market data until a search has actually
    returned rows. `discovery` stays null rather than becoming an empty report,
    which the UI would have to guess how to read."""
    body = client.get("/api/provider/robinhood/status").json()
    assert body["market_data_available"] is False
    assert body["discovery"] is None


def test_nothing_robinhood_exists_before_someone_asks(service):
    """Spec §41/§42: the universe is lazy. Constructing the service must not
    build an RPC client, and boot must not schedule a Robinhood task."""
    assert service._robinhood_rpc is None
    assert service._robinhood_task is None
    assert service.robinhood_chain.state is ChainState.PENDING


def test_binance_status_endpoint_reads_the_feed_verification(client, service):
    service.feed.state = FeedState.VERIFIED
    body = client.get("/api/provider/binance/status").json()
    assert body["provider"] == "BINANCE_SPOT"
    assert body["live_verified"] is True
    assert body["verification"]["state"] == "VERIFIED"


def test_robinhood_failure_state_is_reported_not_raised(client, service):
    service.robinhood_chain = ChainVerification(
        state=ChainState.FAILED, provider="ROBINHOOD-CHAIN-RPC", error="SourceUnavailable"
    )
    body = client.get("/api/provider/robinhood/status").json()
    assert body["verification"]["state"] == "FAILED"
    assert body["verification"]["emoji"] == "🔴"
    assert body["live_verified"] is False


def test_partial_licenses_nothing(client, service):
    """🟡 PARTIEL shows which half is broken; it never counts as verified."""
    service.robinhood_chain = ChainVerification(
        state=ChainState.PARTIAL, provider="ROBINHOOD-CHAIN-RPC"
    )
    body = client.get("/api/provider/robinhood/status").json()
    assert body["verification"]["emoji"] == "🟡"
    assert body["live_verified"] is False


# --------------------------------------------------------------------------- #
# The frontend contract these endpoints exist to serve
# --------------------------------------------------------------------------- #


def _app_source() -> str:
    return (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")


def test_the_switch_reads_the_fresher_of_the_two_robinhood_states():
    """Found by running the app: `/api/providers` is polled every 15 s while the
    chain status is polled every 700 ms, so the switch showed 🟡 "vérification…"
    while the badge directly below it already said 🔴 NON VÉRIFIÉ — two
    renderings of one state, disagreeing on screen (invariant 61).

    The fix is that the switch consumes the overridden list, never the raw one.
    """
    src = _app_source()
    assert "switchProviders" in src
    assert "providers={switchProviders}" in src
    assert "providers={providers}" not in src, "the switch must not read the slow list"


def test_binance_chrome_is_not_rendered_in_the_robinhood_universe():
    """Same run: the topbar kept showing "DEMO · 23s · 18/18 · Scan" — the
    Binance scan's status — while the Robinhood universe was on screen. Every
    Binance-only block is gated on the selected market."""
    src = _app_source()
    for block in ('className="status-compact"', 'className="status-strip"', 'className="bottom-nav"'):
        idx = src.index(block)
        preceding = src[max(0, idx - 260):idx]
        assert "market === 'BINANCE_SPOT'" in preceding, f"{block} is not gated on the market"


# --------------------------------------------------------------------------- #
# Discovery, exposed
# --------------------------------------------------------------------------- #


def test_tokens_endpoint_answers_pending_without_blocking(client):
    body = client.get("/api/robinhood/tokens").json()
    assert body["state"] in ("PENDING", "FAILED", "EMPTY")
    assert body["tokens"] == []


def test_market_data_available_requires_an_actual_search_result(client, service):
    """Not a configured URL, not a live chain — rows. Anything weaker would let
    the UI promise data it does not have."""
    assert client.get("/api/provider/robinhood/status").json()["market_data_available"] is False


def test_the_two_sources_are_reported_separately(client, service):
    """Spec §6 generalised: the RPC proves the chain, the indexer provides the
    market data, and one being healthy says nothing about the other."""
    body = client.get("/api/provider/robinhood/status").json()
    ids = [s["id"] for s in body["sources"]]
    assert ids == ["RPC", "GECKOTERMINAL"]
    assert body["sources"][0]["endpoint"] == "rpc.mainnet.chain.robinhood.com"
    assert body["sources"][1]["endpoint"] == "api.geckoterminal.com"


def test_an_empty_search_is_not_reported_as_a_failure(service):
    """A quiet window and a broken indexer must never render the same: one is a
    fact about the chain, the other about our connection."""
    from cryptopulse.api.service import _discovery_state
    from cryptopulse.hunter.robinhood_discovery import RobinhoodDiscoveryReport

    assert _discovery_state(None) == "PENDING"
    assert _discovery_state(RobinhoodDiscoveryReport()) == "EMPTY"
    assert _discovery_state(RobinhoodDiscoveryReport(errors=["page 1: SourceUnavailable"])) == "FAILED"


def test_nothing_discovery_runs_before_someone_asks(service):
    """Spec §41-42: still lazy after R3. The indexer client is not even built."""
    assert service._gecko is None
    assert service._discovery_task is None
    assert service.robinhood_discovery is None
