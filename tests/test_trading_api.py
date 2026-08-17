"""The trading endpoints, end to end against a real app.

The workflow this covers is the product's whole point:

    the engine recommends  ->  the user acts *manually*  ->  the user confirms
    ->  the position is watched  ->  the exit is recommended  ->  confirmed

Nothing in that chain may place an order, and the last test in this file walks
the routing table asserting no endpoint exists that could.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cryptopulse.api.app import create_app
from cryptopulse.api.service import ScannerService, set_service
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.database import repo
from cryptopulse.database.session import reset_engine


@pytest.fixture()
def client(tmp_path):
    reset_engine()
    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.database.url = f"sqlite:///{tmp_path}/trading.db"
    settings.scanner.min_quote_volume_24h = 100_000.0

    set_service(ScannerService(settings))
    # start_loop=False so the position watcher does not race the assertions.
    with TestClient(create_app(start_loop=False)) as c:
        c.post("/api/scan/run")
        yield c
    set_service(None)
    reset_engine()


def _open(client, symbol="BTCUSDT", **body):
    resp = client.post("/api/positions/open", json={"symbol": symbol, **body})
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #


def test_decisions_are_produced_for_everything_scanned(client):
    body = client.get("/api/decisions").json()

    assert body["entries"], "a completed scan must produce decisions"
    for row in body["entries"]:
        assert row["action"] in ("BUY", "WATCH", "AVOID")
        assert row["emoji"] and row["label_fr"] and row["tone"]


def test_only_entry_decisions_appear_for_things_not_held(client):
    """HOLD, REDUCE and SELL are about a position. Offering them for something
    nobody owns would be meaningless."""
    for row in client.get("/api/decisions").json()["entries"]:
        assert row["action"] not in ("HOLD", "REDUCE", "SELL")


def test_counts_include_the_zeroes(client):
    """A missing key renders as a blank. "No buy signals" is a real answer the
    screen has to be able to state."""
    counts = client.get("/api/decisions").json()["counts"]
    assert set(counts) == {"BUY", "HOLD", "WATCH", "REDUCE", "SELL", "AVOID"}
    assert all(isinstance(v, int) for v in counts.values())


def test_the_decisions_payload_says_no_order_is_ever_placed(client):
    body = client.get("/api/decisions").json()
    assert "Aucun ordre" in body["disclaimer"]
    assert body["engine"]["weights_fingerprint"]


def test_a_held_symbol_leaves_the_entry_list(client):
    """What to do about something you own is the watcher's question, answered at
    the watcher's cadence — not a once-a-minute entry decision."""
    _open(client, "BTCUSDT")
    client.post("/api/scan/run")

    entries = client.get("/api/decisions").json()["entries"]
    assert all(row["symbol"] != "BTCUSDT" for row in entries)


# --------------------------------------------------------------------------- #
# Opening — always a user action
# --------------------------------------------------------------------------- #


def test_opening_a_position_records_the_entry_context(client):
    position = _open(client)

    assert position["status"] == "OPEN"
    assert position["entry_price"] is not None
    assert position["entry_opportunity"] is not None
    assert position["peak_price"] == position["entry_price"]
    assert position["pnl_basis"] == "observed_price"


def test_fill_details_are_optional_and_change_the_basis(client):
    position = _open(client, actual_entry_price=123.45, amount_invested=500.0, quantity=4.05)

    assert position["actual_entry_price"] == 123.45
    assert position["pnl_basis"] == "actual_fill"
    assert position["amount_invested"] == 500.0


def test_opening_a_symbol_the_scanner_has_never_seen_needs_a_price(client):
    """Rather than inventing one. An entry price the user never saw is the most
    dangerous number this application could store."""
    resp = client.post("/api/positions/open", json={"symbol": "NOSUCHTOKEN"})
    assert resp.status_code == 400
    assert "aucun prix connu" in resp.json()["detail"]

    ok = client.post(
        "/api/positions/open", json={"symbol": "NOSUCHTOKEN", "entry_price": 1.5}
    )
    assert ok.status_code == 200


def test_opening_from_a_signal_answers_it_and_links_the_position(client):
    signal = repo.save_trade_signal({
        "symbol": "BTCUSDT", "action": "BUY", "timestamp_ms": 1_700_000_000_000,
        "price": 60_000.0, "trigger_price": 61_000.0, "invalidation_price": 59_000.0,
        "opportunity_score": 84.0, "engine_version": "TRADE_DECISION_V1",
    })
    position = _open(client, signal_id=signal["id"])

    assert position["signal_id"] == signal["id"]
    # The signal's prices win over the live scan: they are what the user saw.
    assert position["entry_price"] == 60_000.0
    assert position["invalidation_price"] == 59_000.0

    answered = client.get("/api/trade-signals").json()["signals"][0]
    assert answered["taken"] is True
    assert answered["position_id"] == position["id"]


# --------------------------------------------------------------------------- #
# Answering, including no
# --------------------------------------------------------------------------- #


def test_a_refused_signal_is_kept_and_followed(client):
    signal = repo.save_trade_signal({
        "symbol": "ETHUSDT", "action": "BUY", "timestamp_ms": 1_700_000_000_000,
        "price": 3000.0, "opportunity_score": 80.0, "engine_version": "TRADE_DECISION_V1",
    })
    answered = client.post(
        f"/api/trade-signals/{signal['id']}/answer", json={"taken": False}
    ).json()

    assert answered["taken"] is False
    assert answered["opportunity_score"] == 80.0, "the context survives a refusal"


def test_an_unanswered_signal_is_surfaced_rather_than_lost(client):
    repo.save_trade_signal({
        "symbol": "ETHUSDT", "action": "BUY", "timestamp_ms": 1_700_000_000_000,
        "price": 3000.0, "engine_version": "TRADE_DECISION_V1",
    })
    awaiting = client.get("/api/decisions").json()["awaiting_answer"]
    assert [row["symbol"] for row in awaiting] == ["ETHUSDT"]
    assert awaiting[0]["taken"] is None


def test_answering_an_unknown_signal_is_a_404(client):
    resp = client.post("/api/trade-signals/9999/answer", json={"taken": True})
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Watching and closing
# --------------------------------------------------------------------------- #


def test_the_watcher_states_exactly_what_it_cost(client):
    """A loop that quietly spent hundreds of requests would be discovered as a
    rate-limit ban rather than as a number on a screen."""
    _open(client)
    report = client.post("/api/positions/watch").json()

    assert report["checked"] == 1
    assert "requests_used" in report
    assert report["errors"] == {}


def test_with_nothing_open_the_watcher_costs_nothing(client):
    report = client.post("/api/positions/watch").json()
    assert report["checked"] == 0
    assert report["requests_used"] == 0
    assert any("aucune position" in n for n in report["notes"])


def test_watching_a_symbol_the_scan_already_covered_does_not_pay_twice(client):
    """The same rule the deep scan follows. Paying twice for an identical answer
    is how a request budget disappears."""
    _open(client)
    report = client.post("/api/positions/watch").json()
    assert report["requests_used"] == 0, "the scan had already analysed this symbol"


def test_a_watch_pass_records_a_decision_for_the_position(client):
    position = _open(client)
    client.post("/api/positions/watch")

    current = client.get("/api/positions").json()["positions"][0]
    assert current["current_decision"] in ("HOLD", "WATCH", "REDUCE", "SELL")
    assert current["health_score"] is not None or current["current_decision"] == "WATCH"

    events = client.get(f"/api/positions/{position['id']}/events").json()["events"]
    assert len(events) == 1
    assert events[0]["previous_decision"] is None


def test_closing_uses_the_observed_price_when_no_fill_is_given(client):
    position = _open(client)
    closed = client.post(f"/api/positions/{position['id']}/close", json={}).json()

    assert closed["status"] == "CLOSED"
    assert closed["exit_price"] is not None
    assert closed["realised_pnl_pct"] is not None


def test_closing_with_a_real_fill_uses_it(client):
    position = _open(client, actual_entry_price=100.0)
    closed = client.post(
        f"/api/positions/{position['id']}/close",
        json={"actual_exit_price": 110.0, "reason": "support cassé"},
    ).json()

    assert closed["realised_pnl_pct"] == pytest.approx(10.0, abs=0.01)
    assert closed["close_reason"] == "support cassé"


def test_a_closed_position_leaves_the_open_list(client):
    position = _open(client)
    client.post(f"/api/positions/{position['id']}/close", json={})

    assert client.get("/api/positions").json()["positions"] == []
    assert client.get("/api/positions?status=CLOSED").json()["positions"]


def test_closing_an_unknown_position_is_a_404(client):
    assert client.post("/api/positions/9999/close", json={}).status_code == 404


# --------------------------------------------------------------------------- #
# The rule the whole product rests on
# --------------------------------------------------------------------------- #


def test_no_endpoint_can_place_an_order(client):
    """Checked against the live routing table rather than trusted. This is the
    surface where an order endpoint would have to appear to exist at all."""
    paths = [r.path for r in client.app.routes]
    forbidden = ("order", "buy", "sell", "trade/execute", "withdraw", "transfer")
    offenders = [
        p for p in paths
        if any(f in p.lower() for f in forbidden) and not p.startswith("/api/trade-signals")
    ]
    assert not offenders, offenders


def test_paper_mode_stays_on(client):
    assert client.get("/api/health").json()["paper_mode"] is True


def test_a_signal_for_another_token_is_refused(client):
    """Found by opening a position with the wrong signal id. Without this the
    merge imported another token's entry price and invalidation, and the
    position was then judged for its whole life against a different chart."""
    signal = repo.save_trade_signal({
        "symbol": "WIFUSDT", "action": "BUY", "timestamp_ms": 1_700_000_000_000,
        "price": 2.15, "invalidation_price": 2.05, "engine_version": "TRADE_DECISION_V1",
    })
    resp = client.post(
        "/api/positions/open", json={"symbol": "BTCUSDT", "signal_id": signal["id"]}
    )

    assert resp.status_code == 400
    assert "WIFUSDT" in resp.json()["detail"]
    assert client.get("/api/positions").json()["positions"] == []


# --------------------------------------------------------------------------- #
# Results — two questions, deliberately not merged
# --------------------------------------------------------------------------- #


def test_results_separate_the_engine_from_the_user(client):
    """Did the engine recommend well, and did the user do well, are different
    numbers. Someone can follow good signals badly or ignore them profitably."""
    body = client.get("/api/results").json()

    assert "positions" in body and "signals" in body
    assert "taken_vs_skipped" in body
    assert "sell_signals" in body


def test_no_rate_is_shown_below_the_sample_floor(client):
    """A win rate over four positions is not a finding about anyone's trading,
    and this feature is days old — 'insufficient sample' is the correct output
    for months, not a failure."""
    position = _open(client)
    client.post(f"/api/positions/{position['id']}/close", json={})

    overall = client.get("/api/results").json()["positions"]["overall"]
    assert overall["n"] == 1
    assert overall["insufficient_sample"] is True
    assert overall["win_rate"] is None
    assert overall["profit_factor"] is None


def test_unanswered_signals_are_counted_apart_from_refused_ones(client):
    """Folding them together would turn every prompt nobody got round to into a
    deliberate refusal."""
    repo.save_trade_signal({
        "symbol": "ETHUSDT", "action": "BUY", "timestamp_ms": 1_700_000_000_000,
        "price": 3000.0, "engine_version": "TRADE_DECISION_V1",
    })
    refused = repo.save_trade_signal({
        "symbol": "SOLUSDT", "action": "BUY", "timestamp_ms": 1_700_000_000_000,
        "price": 150.0, "engine_version": "TRADE_DECISION_V1",
    })
    repo.answer_trade_signal(refused["id"], False)

    signals = client.get("/api/results").json()["signals"]
    assert signals["unanswered"] == 1
    assert signals["skipped"] == 1
    assert signals["follow_rate"] == 0.0, "one refusal out of one answered"


def test_the_sell_table_states_that_its_sign_is_inverted(client):
    """A sell was right when the price fell afterwards. Reading that table with
    a buy's intuition would invert the conclusion."""
    note = client.get("/api/results").json()["sell_signals"]["note"]
    assert "BAISSÉ" in note


def test_results_never_grade_the_user(client):
    """The most misleading number this product could produce, and the one people
    would remember."""
    body = client.get("/api/results").json()
    assert any("ne juge pas votre flair" in n for n in body["notes"])
