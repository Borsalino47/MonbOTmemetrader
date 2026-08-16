"""Manual validation — the only human judgements this system stores.

Everything else in the journal records what the software thought. These rows
record what the person decided, which makes them the one dataset capable of
answering "does the user outperform the scanner, or the other way round?".

Three properties have to hold for that to be worth anything later:

  1. The row is a photograph of the screen, not a pointer to a scan. Context is
     copied in, so a decision stays readable after the scan behind it is pruned
     and after the engine that explained it has been reweighted.
  2. The table is append-only. A sequence of decisions on one token is the thing
     worth studying; flattening it to a final state would erase it.
  3. A decision taken in DEMO mode is marked and counted apart. Pooling it with
     real ones would put judgements about generated candles into a statistic
     about someone's judgement of a market.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cryptopulse.api.app import create_app
from cryptopulse.api.service import ScannerService, set_service
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.database.session import reset_engine


@pytest.fixture()
def client(tmp_path):
    reset_engine()
    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.database.url = f"sqlite:///{tmp_path}/validations.db"
    settings.scanner.min_quote_volume_24h = 100_000.0

    set_service(ScannerService(settings))
    with TestClient(create_app(start_loop=False)) as c:
        c.post("/api/scan/run")
        yield c
    set_service(None)
    reset_engine()


def _post(client, **kwargs):
    body = {"symbol": "BTCUSDT", "decision": "VALIDATED", **kwargs}
    resp = client.post("/api/validations", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# The photograph
# --------------------------------------------------------------------------- #


def test_a_decision_records_the_screen_it_was_made_from(client):
    """Not a foreign key. A signal row exists only for OBSERVE and above, an old
    scan can be pruned, and a reweighted engine would re-explain a past decision
    in words the user never read."""
    row = _post(client)

    assert row["symbol"] == "BTCUSDT"
    assert row["decision"] == "VALIDATED"
    assert row["price"] is not None
    assert row["final_score"] is not None
    assert row["setup_state"] is not None
    assert row["engine_version"]
    assert row["signal_timestamp_ms"], "the decision must know which bar it was about"
    assert row["why"], "the sentences the user read are part of the record"


def test_the_client_view_wins_over_the_servers_current_state(client):
    """A scan can land between the render and the tap. What the user decided
    against is what was on their screen, so the client's numbers are kept."""
    row = _post(client, price=12345.0, final_score=99.0, note="what I saw")

    assert row["price"] == 12345.0
    assert row["final_score"] == 99.0
    assert row["note"] == "what I saw"


def test_context_the_client_omits_is_filled_from_the_scan_not_from_zero(client):
    row = _post(client, decision="WATCHLIST")
    assert row["pump_maturity"] is not None
    assert row["data_confidence"] is not None


def test_a_field_that_is_unknown_everywhere_stays_null(client):
    """The discovery score only exists after a deep scan. A decision taken
    without one must not record a zero, which would later read as 'validated a
    token the discovery engine rated at zero' — a statement nobody made."""
    row = _post(client)
    assert row["discovery_score"] is None


def test_a_symbol_absent_from_the_scan_can_still_be_decided_on(client):
    """Rejecting something is a decision worth keeping even when the scanner has
    nothing to say about it."""
    resp = client.post(
        "/api/validations",
        json={
            "symbol": "NOTINSCAN", "decision": "REJECTED",
            "price": 1.5, "signal_timestamp_ms": 1_700_000_000_000,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["symbol"] == "NOTINSCAN"
    assert resp.json()["final_score"] is None


# --------------------------------------------------------------------------- #
# Append-only
# --------------------------------------------------------------------------- #


def test_changing_your_mind_appends_rather_than_overwrites(client):
    first = _post(client, decision="VALIDATED")
    second = _post(client, decision="REJECTED")

    assert first["id"] != second["id"]
    history = client.get("/api/validations?symbol=BTCUSDT").json()["validations"]
    assert [r["decision"] for r in history] == ["REJECTED", "VALIDATED"]


def test_the_current_state_is_derived_from_the_history_not_stored(client):
    """A stored 'current decision' could drift from the history behind it. This
    one is a view over it and cannot."""
    _post(client, decision="VALIDATED")
    _post(client, decision="WATCHLIST")

    current = client.get("/api/validations/current").json()["current"]
    assert current["BTCUSDT"]["decision"] == "WATCHLIST"


def test_counts_cover_every_decision_type_including_the_unused_ones(client):
    """A missing key would render as a blank rather than as a zero on screen."""
    _post(client, decision="VALIDATED")
    body = client.get("/api/validations").json()

    assert set(body["counts"]) == set(body["decisions"])
    assert body["counts"]["VALIDATED"] == 1
    assert body["counts"]["ANALYSE"] == 0
    assert body["total"] == 1


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_an_unknown_decision_is_refused_rather_than_stored(client):
    resp = client.post("/api/validations", json={"symbol": "BTCUSDT", "decision": "MAYBE"})
    assert resp.status_code == 400
    assert "MAYBE" in resp.json()["detail"]


def test_decisions_taken_in_demo_mode_are_marked_and_counted_apart(client):
    """The fixture provider generates its candles. A judgement about a generated
    chart says nothing about anyone's judgement of a market, and the two must
    never end up in the same statistic."""
    row = _post(client)
    assert row["synthetic"] is True
    assert row["data_source"] == "SYNTHETIC-FIXTURE"

    body = client.get("/api/validations").json()
    assert body["synthetic"] == body["total"]
    assert "DEMO" in body["note"]


def test_an_outcome_is_never_written_at_decision_time(client):
    """What followed a decision is not knowable when it is taken. Writing it
    would be look-ahead committed straight to disk."""
    row = _post(client)
    assert row["outcome"]["evaluated"] is False
    assert row["outcome"]["price"] is None
    assert row["outcome"]["change_pct"] is None


def test_a_long_note_is_clipped_rather_than_rejected(client):
    """Losing a decision because someone typed too much would be worse than
    losing the tail of their sentence."""
    row = _post(client, note="x" * 900)
    assert row["note"] is not None
    assert len(row["note"]) <= 400


def test_retention_never_deletes_a_users_own_decision(client):
    """`prune` is the only destructive operation in the system, and it is bounded
    to what the scanner produced. A person's judgement is not operational data:
    it is low volume, it cannot be recomputed, and it is the one thing here that
    could not be regenerated by running the scanner again."""
    _post(client, decision="VALIDATED")
    _post(client, decision="WATCHLIST", symbol="ETHUSDT")

    removed = client.post("/api/maintenance/prune").json()["removed"]
    assert "validations" not in removed

    assert client.get("/api/validations").json()["total"] == 2
