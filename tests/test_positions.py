"""Positions, trade signals, and the gate that stops the screen flickering.

Three properties are load-bearing here.

**A skipped signal is followed too.** Recording only what the user acted on
would make the journal systematically flattering: the signals someone skipped
are exactly the ones they were unsure about, and dropping them means never
finding out whether the doubt was justified.

**Unanswered is not "no".** `taken` has three states, and folding NULL into
False would count every prompt nobody got round to as a deliberate refusal.

**A move toward the exit is never delayed.** The cooldown that stops the screen
oscillating must not be the reason someone got out late.
"""

from __future__ import annotations

import pytest

from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.database import repo
from cryptopulse.database.session import init_engine, reset_engine
from cryptopulse.trading.decision import TradeAction
from cryptopulse.trading.hysteresis import DecisionGate, is_escalation

NOW_MS = 1_700_000_000_000
MINUTE = 60_000


@pytest.fixture()
def db(tmp_path):
    reset_engine()
    settings = CryptoPulseSettings()
    settings.database.url = f"sqlite:///{tmp_path}/positions.db"
    init_engine(settings.database)
    yield
    reset_engine()


def _signal(**kwargs) -> dict:
    body = {
        "symbol": "SUIUSDT", "action": "BUY", "strength": "VERY_STRONG",
        "timestamp_ms": NOW_MS, "price": 0.8421,
        "opportunity_score": 84.0, "explosion_score": 89.0, "safety_score": 94.0,
        "data_confidence": 100.0, "pump_maturity": 21.0,
        "trigger_price": 0.8460, "invalidation_price": 0.8290,
        "reasons": ["volume en forte accélération"], "risks": ["résistance proche"],
        "engine_version": "TRADE_DECISION_V1", "weights_fingerprint": "abc123",
        "data_source": "SYNTHETIC-FIXTURE", "synthetic": True,
        **kwargs,
    }
    return repo.save_trade_signal(body)


def _position(signal_id=None, **kwargs) -> dict:
    body = {
        "symbol": "SUIUSDT", "opened_ms": NOW_MS, "entry_price": 0.8421,
        "signal_id": signal_id, "trigger_price": 0.8460, "invalidation_price": 0.8290,
        "entry_opportunity": 84.0, "entry_explosion": 89.0,
        "entry_reasons": ["volume en forte accélération"],
        "data_source": "SYNTHETIC-FIXTURE", "synthetic": True,
        **kwargs,
    }
    return repo.open_position(body)


# --------------------------------------------------------------------------- #
# Trade signals
# --------------------------------------------------------------------------- #


def test_a_new_signal_starts_unanswered_not_refused(db):
    """NULL is a third state. Folding it into False would count every prompt
    nobody got round to as a deliberate refusal."""
    signal = _signal()
    assert signal["taken"] is None
    assert signal["answered_at"] is None
    assert repo.unanswered_trade_signals() == [signal]


def test_answering_yes_and_no_are_both_recorded(db):
    yes = repo.answer_trade_signal(_signal()["id"], True)
    no = repo.answer_trade_signal(_signal(symbol="XAIUSDT")["id"], False)

    assert yes["taken"] is True
    assert no["taken"] is False
    assert no["answered_at"] is not None


def test_a_skipped_signal_is_kept_with_its_full_context(db):
    """The counterfactual is the point: this row is what lets the app say later
    what would have happened if the user had bought."""
    signal = repo.answer_trade_signal(_signal()["id"], False)

    assert signal["taken"] is False
    assert signal["opportunity_score"] == 84.0
    assert signal["invalidation_price"] == 0.8290
    assert signal["reasons"]


def test_an_answer_cannot_be_rewritten(db):
    """The question 'did you act on this?' has one true answer. Letting it be
    changed later would turn the taken-vs-skipped comparison into a record of
    how someone feels about their past decisions."""
    signal = _signal()
    repo.answer_trade_signal(signal["id"], True)
    again = repo.answer_trade_signal(signal["id"], False)
    assert again["taken"] is True


def test_only_buy_and_sell_are_trade_signals(db):
    with pytest.raises(ValueError, match="BUY or SELL"):
        _signal(action="WATCH")


def test_an_outcome_is_never_written_when_a_signal_is_emitted(db):
    """The outcome of a recommendation is not knowable at the moment it is
    made. Writing it would be look-ahead committed straight to disk."""
    outcome = _signal()["outcome"]
    assert outcome["evaluated"] is False
    assert all(outcome[k] is None for k in outcome if k != "evaluated")


# --------------------------------------------------------------------------- #
# Positions
# --------------------------------------------------------------------------- #


def test_a_position_starts_at_its_entry_with_no_gain_or_loss(db):
    """Peak and trough start at the entry rather than NULL: a position that has
    never been observed since entry still has a peak — it is where it started."""
    position = _position()

    assert position["status"] == "OPEN"
    assert position["peak_price"] == position["entry_price"]
    assert position["mfe_pct"] == 0.0
    assert position["pnl_pct"] == 0.0


def test_a_position_copies_the_entry_context_it_will_be_judged_against(db):
    """The health score compares now against this. A snapshot recomputed later
    would always agree with itself."""
    position = _position()
    assert position["entry_opportunity"] == 84.0
    assert position["invalidation_price"] == 0.8290
    assert position["entry_reasons"]


def test_optional_fill_details_are_optional(db):
    """Someone who does not want to record what they paid should still get
    position tracking, and which price the returns use must be visible."""
    observed = _position()
    with_fill = _position(symbol="XAIUSDT", actual_entry_price=0.85, amount_invested=100.0)

    assert observed["actual_entry_price"] is None
    assert observed["pnl_basis"] == "observed_price"
    assert with_fill["pnl_basis"] == "actual_fill"


def test_peak_and_trough_only_ever_widen(db):
    """A watcher that misses a cycle loses resolution, never a record. The worst
    case is an MFE slightly understated, never a fabricated one."""
    position = _position()
    repo.update_position(position["id"], price=0.95, now_ms=NOW_MS + MINUTE)
    repo.update_position(position["id"], price=0.80, now_ms=NOW_MS + 2 * MINUTE)
    latest = repo.update_position(position["id"], price=0.88, now_ms=NOW_MS + 3 * MINUTE)

    assert latest["peak_price"] == 0.95
    assert latest["trough_price"] == 0.80
    assert latest["last_price"] == 0.88
    assert latest["mfe_pct"] == pytest.approx(12.81, abs=0.1)
    assert latest["mae_pct"] == pytest.approx(-5.0, abs=0.1)


def test_drawdown_is_measured_from_the_peak_not_from_the_entry(db):
    """A position up 6% that was up 13% has given back half its gain, and that
    is the number worth acting on."""
    position = _position()
    repo.update_position(position["id"], price=0.95, now_ms=NOW_MS + MINUTE)
    latest = repo.update_position(position["id"], price=0.89, now_ms=NOW_MS + 2 * MINUTE)

    assert latest["pnl_pct"] > 0
    assert latest["drawdown_from_peak_pct"] == pytest.approx(-6.32, abs=0.1)


def test_closing_records_the_realised_return_and_is_idempotent(db):
    position = _position()
    closed = repo.close_position(
        position["id"], exit_price=0.881, now_ms=NOW_MS + 10 * MINUTE, reason="support cassé"
    )
    assert closed["status"] == "CLOSED"
    assert closed["realised_pnl_pct"] == pytest.approx(4.62, abs=0.05)

    again = repo.close_position(position["id"], exit_price=0.5, now_ms=NOW_MS + 20 * MINUTE)
    assert again["realised_pnl_pct"] == closed["realised_pnl_pct"]


def test_a_real_exit_price_overrides_the_observed_one(db):
    position = _position(actual_entry_price=0.84)
    closed = repo.close_position(
        position["id"], exit_price=0.90, actual_exit_price=0.92, now_ms=NOW_MS + MINUTE
    )
    assert closed["realised_pnl_pct"] == pytest.approx(9.52, abs=0.05)


def test_the_watcher_only_has_to_look_at_open_positions(db):
    _position()
    other = _position(symbol="XAIUSDT")
    repo.close_position(other["id"], exit_price=1.0, now_ms=NOW_MS + MINUTE)

    assert repo.open_position_symbols() == ["SUIUSDT"]


# --------------------------------------------------------------------------- #
# The decision history
# --------------------------------------------------------------------------- #


def test_only_changes_are_recorded(db):
    """Writing a row every cycle would bury the five moments that matter under
    a thousand that do not."""
    position = _position()
    first = repo.record_position_decision(
        position["id"], {"decision": "HOLD", "price": 0.858}, now_ms=NOW_MS + MINUTE
    )
    repeat = repo.record_position_decision(
        position["id"], {"decision": "HOLD", "price": 0.891}, now_ms=NOW_MS + 2 * MINUTE
    )

    assert first is not None
    assert repeat is None
    assert len(repo.position_events(position["id"])) == 1


def test_the_sequence_is_kept_in_order_with_what_preceded_each_step(db):
    """This is what lets someone reconstruct *when* the case fell apart rather
    than merely that it did."""
    position = _position()
    for decision, price, ms in [
        ("HOLD", 0.858, 1), ("WATCH", 0.904, 2), ("REDUCE", 0.896, 3), ("SELL", 0.881, 4),
    ]:
        repo.record_position_decision(
            position["id"], {"decision": decision, "price": price}, now_ms=NOW_MS + ms * MINUTE
        )

    events = repo.position_events(position["id"])
    assert [e["decision"] for e in events] == ["HOLD", "WATCH", "REDUCE", "SELL"]
    assert events[-1]["previous_decision"] == "REDUCE"
    assert events[0]["price"] == 0.858


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #


def test_the_first_reading_is_shown_immediately():
    gate = DecisionGate()
    outcome = gate.evaluate("SUIUSDT", TradeAction.HOLD, now_ms=NOW_MS)
    assert outcome.changed is True
    assert outcome.action is TradeAction.HOLD


def test_a_single_cycle_flicker_never_reaches_the_screen():
    """The most common noise by a wide margin, and the one that destroys trust
    in every other reading."""
    gate = DecisionGate(confirmations_required=2)
    gate.evaluate("SUIUSDT", TradeAction.HOLD, now_ms=NOW_MS)
    flicker = gate.evaluate("SUIUSDT", TradeAction.SELL, now_ms=NOW_MS + 1000)

    assert flicker.action is TradeAction.HOLD
    assert flicker.changed is False
    assert "confirmation" in flicker.held_reason


def test_a_repeated_reading_gets_through():
    gate = DecisionGate(confirmations_required=2)
    gate.evaluate("SUIUSDT", TradeAction.HOLD, now_ms=NOW_MS)
    gate.evaluate("SUIUSDT", TradeAction.SELL, now_ms=NOW_MS + 1000)
    confirmed = gate.evaluate("SUIUSDT", TradeAction.SELL, now_ms=NOW_MS + 2000)

    assert confirmed.action is TradeAction.SELL
    assert confirmed.changed is True


def test_an_interrupted_run_of_confirmations_starts_over():
    """Otherwise a flicker every other cycle would accumulate into a change."""
    gate = DecisionGate(confirmations_required=3)
    gate.evaluate("SUIUSDT", TradeAction.HOLD, now_ms=NOW_MS)
    gate.evaluate("SUIUSDT", TradeAction.SELL, now_ms=NOW_MS + 1000)
    gate.evaluate("SUIUSDT", TradeAction.HOLD, now_ms=NOW_MS + 2000)
    third = gate.evaluate("SUIUSDT", TradeAction.SELL, now_ms=NOW_MS + 3000)

    assert third.changed is False
    assert third.confirmations == 1


def test_the_cooldown_holds_a_move_back_toward_calm():
    """Confirmation alone does not stop a slow oscillation: a decision flipping
    every three cycles would confirm every time.

    The direction tested here is deliberate. Going *back* to a calmer decision is
    the one that must wait — an improvement that turns out to be noise is how the
    screen starts contradicting itself, and nothing is lost by taking five
    minutes to believe it.
    """
    gate = DecisionGate(confirmations_required=1, min_seconds_between_changes=300)
    gate.evaluate("SUIUSDT", TradeAction.WATCH, now_ms=NOW_MS)
    calmer = gate.evaluate("SUIUSDT", TradeAction.HOLD, now_ms=NOW_MS + 1000)

    assert calmer.changed is False
    assert calmer.action is TradeAction.WATCH
    assert "moins de 300s" in calmer.held_reason


def test_a_step_toward_the_exit_is_an_escalation_even_when_it_is_only_watch():
    """HOLD -> WATCH means something started deteriorating. Delaying that is the
    same class of mistake as delaying a sell, just smaller."""
    gate = DecisionGate(confirmations_required=1, min_seconds_between_changes=3600)
    gate.evaluate("SUIUSDT", TradeAction.HOLD, now_ms=NOW_MS)
    worse = gate.evaluate("SUIUSDT", TradeAction.WATCH, now_ms=NOW_MS + 1000)

    assert worse.changed is True
    assert is_escalation(TradeAction.HOLD, TradeAction.WATCH) is True


def test_the_cooldown_never_delays_a_move_toward_the_exit():
    """Getting out late because a timer was running is not a trade-off worth
    making."""
    gate = DecisionGate(confirmations_required=1, min_seconds_between_changes=3600)
    gate.evaluate("SUIUSDT", TradeAction.HOLD, now_ms=NOW_MS)
    gate.evaluate("SUIUSDT", TradeAction.WATCH, now_ms=NOW_MS + 1000)
    out = gate.evaluate("SUIUSDT", TradeAction.SELL, now_ms=NOW_MS + 2000)

    assert out.changed is True
    assert out.action is TradeAction.SELL


def test_an_override_bypasses_confirmation_and_cooldown_alike():
    """Waiting two cycles to mention a broken invalidation would be
    indefensible."""
    gate = DecisionGate(confirmations_required=5, min_seconds_between_changes=3600)
    gate.evaluate("SUIUSDT", TradeAction.HOLD, now_ms=NOW_MS)
    urgent = gate.evaluate(
        "SUIUSDT", TradeAction.SELL, now_ms=NOW_MS + 500,
        override_reason="invalidation franchie",
    )

    assert urgent.changed is True
    assert urgent.action is TradeAction.SELL
    assert urgent.override == "invalidation franchie"


def test_holding_and_watching_are_shown_without_delay():
    """Telling someone to keep watching costs nothing. Only the decisions that
    cost money need to earn their place."""
    gate = DecisionGate(confirmations_required=3, min_seconds_between_changes=0)
    gate.evaluate("SUIUSDT", TradeAction.HOLD, now_ms=NOW_MS)
    watch = gate.evaluate("SUIUSDT", TradeAction.WATCH, now_ms=NOW_MS + 1000)
    assert watch.changed is True


def test_no_oscillation_survives_the_gate():
    """The scenario the whole module exists for: alternating readings must not
    produce alternating screens."""
    gate = DecisionGate(confirmations_required=2, min_seconds_between_changes=300)
    gate.evaluate("SUIUSDT", TradeAction.HOLD, now_ms=NOW_MS)

    changes = 0
    for i in range(1, 21):
        proposed = TradeAction.SELL if i % 2 else TradeAction.HOLD
        if gate.evaluate("SUIUSDT", proposed, now_ms=NOW_MS + i * 1000).changed:
            changes += 1

    assert changes == 0, "twenty alternating readings must not move the screen once"


def test_symbols_do_not_gate_each_other():
    gate = DecisionGate(confirmations_required=1, min_seconds_between_changes=3600)
    gate.evaluate("SUIUSDT", TradeAction.HOLD, now_ms=NOW_MS)
    other = gate.evaluate("XAIUSDT", TradeAction.SELL, now_ms=NOW_MS)
    assert other.changed is True


def test_closing_a_position_clears_its_memory():
    """The next position in the same token starts from nothing, not from the
    state the previous one ended in."""
    gate = DecisionGate()
    gate.evaluate("SUIUSDT", TradeAction.SELL, now_ms=NOW_MS)
    gate.forget("SUIUSDT")
    assert gate.current("SUIUSDT") is None


def test_escalation_is_measured_toward_the_exit():
    assert is_escalation(TradeAction.HOLD, TradeAction.SELL) is True
    assert is_escalation(TradeAction.REDUCE, TradeAction.SELL) is True
    assert is_escalation(TradeAction.SELL, TradeAction.HOLD) is False
    assert is_escalation(None, TradeAction.SELL) is False


def test_the_peak_and_trough_start_at_the_price_the_returns_use(db):
    """Found by running it. Seeding them from the observed price while computing
    the returns from the fill made "perte max" render as +75.79% — a maximum
    loss that was a gain. Both must start at the same basis so MFE and MAE mean
    what their names say."""
    position = _position(entry_price=2.1692, actual_entry_price=1.2340)

    assert position["peak_price"] == 1.2340
    assert position["trough_price"] == 1.2340
    assert position["mfe_pct"] == 0.0
    assert position["mae_pct"] == 0.0

    fell = repo.update_position(position["id"], price=1.10, now_ms=NOW_MS + MINUTE)
    assert fell["mae_pct"] < 0, "a maximum loss is never positive"
