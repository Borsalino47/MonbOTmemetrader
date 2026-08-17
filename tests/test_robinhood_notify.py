"""Which Robinhood decisions reach the phone, and which must never.

A notification is the only surface this product has when nobody is looking at
the screen, and it is spent from a fixed budget: a phone that vibrates for
things that do not matter gets its notifications switched off, and then the one
that mattered is missed too. So the tests here are about restraint in one
direction and about never being late in the other.

  * ⚫ and 🟡 never notify — they are most of a chain;
  * 🟢 notifies once, not every cycle;
  * 🔴 and 🟠 on something held bypass every rate limit;
  * a delivery failure costs a notification and never a cycle.
"""

from __future__ import annotations

import pytest

from cryptopulse.alerts.robinhood_notify import (
    RobinhoodEntryGate,
    entry_notice,
    position_notice,
    worth_notifying_entry,
    worth_notifying_position,
)
from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.hunter.robinhood_discovery import TokenCandidate
from cryptopulse.providers.geckoterminal import PoolSnapshot, TokenRef
from cryptopulse.risk.robinhood_safety import RugRisk, SafetyReport
from cryptopulse.scoring.robinhood_early import DataConfidence, EarlyScore, PumpMaturity
from cryptopulse.scoring.robinhood_explosion import RobinhoodExplosion
from cryptopulse.trading.decision import TradeAction
from cryptopulse.trading.robinhood_decision import RobinhoodTradeDecisionEngine

SETTINGS = RobinhoodSettings()
ADDR = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
NOW = 1_700_000_000_000


def buyable() -> TokenCandidate:
    c = TokenCandidate(address=ADDR, symbol="FRONG", name="Frong")
    pool = PoolSnapshot(
        pool_address="0xpool", name="FRONG / WETH", dex="uniswap-v4",
        base=TokenRef(address=ADDR, symbol="FRONG"),
        quote=TokenRef(address="0x" + "ee" * 20, symbol="WETH"),
        created_at_ms=None, base_price_usd=0.00042, liquidity_usd=60_000.0,
    )
    c.pools = [pool]
    c.price_usd = 0.00042
    c.liquidity_usd = 60_000.0
    c.pool_age_seconds = 1800.0
    c.safety = SafetyReport(address=ADDR, score=95.0, rug_risk=RugRisk.LOW,
                            analysed=True, coverage=0.9)
    c.early = EarlyScore(score=88.0)
    c.explosion = RobinhoodExplosion(score=90.0, reasons=["poussée d'achats sur 5 minutes"])
    c.confidence = DataConfidence(score=85.0, present=9, total=10)
    c.maturity = PumpMaturity(score=15.0, known=True)
    c.decision = RobinhoodTradeDecisionEngine(SETTINGS).decide_entry(c, now_ms=NOW)
    return c


# --------------------------------------------------------------------------- #
# What is worth an interruption
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "action,expected",
    [("BUY", True), ("WATCH", False), ("AVOID", False), ("HOLD", False), ("SELL", False)],
)
def test_only_a_buy_interrupts_someone_about_a_token_they_do_not_hold(action, expected):
    """⚫ is the ordinary answer for most of a permissionless chain, and 🟡 is
    an invitation to look rather than an instruction."""
    assert worth_notifying_entry(action) is expected


@pytest.mark.parametrize(
    "action,expected",
    [("SELL", True), ("REDUCE", True), ("HOLD", False), ("WATCH", False), ("BUY", False)],
)
def test_only_an_exit_interrupts_someone_about_a_token_they_hold(action, expected):
    """Protecting a gain has a deadline too, which is why RÉDUIRE is in."""
    assert worth_notifying_position(action) is expected


# --------------------------------------------------------------------------- #
# The anti-spam gate (invariant 44)
# --------------------------------------------------------------------------- #


def test_a_token_that_stays_a_buy_notifies_once():
    gate = RobinhoodEntryGate()
    assert gate.allow(ADDR, "BUY") is True
    assert gate.allow(ADDR, "BUY") is False
    assert gate.allow(ADDR, "BUY") is False


def test_a_token_that_stops_being_a_buy_and_becomes_one_again_is_news():
    gate = RobinhoodEntryGate()
    assert gate.allow(ADDR, "BUY") is True
    assert gate.allow(ADDR, "WATCH") is False
    assert gate.allow(ADDR, "BUY") is True


def test_the_gate_is_keyed_on_the_address_not_the_symbol():
    """Symbols are not unique on a permissionless chain, and two tokens calling
    themselves FRONG silencing each other is exactly the copycat's trick."""
    gate = RobinhoodEntryGate()
    assert gate.allow(ADDR, "BUY") is True
    assert gate.allow(OTHER, "BUY") is True


def test_the_gate_is_case_insensitive_about_addresses():
    gate = RobinhoodEntryGate()
    assert gate.allow(ADDR.upper(), "BUY") is True
    assert gate.allow(ADDR.lower(), "BUY") is False


def test_forgetting_a_token_re_arms_it():
    gate = RobinhoodEntryGate()
    gate.allow(ADDR, "BUY")
    gate.forget(ADDR)
    assert gate.allow(ADDR, "BUY") is True


def test_a_non_buy_still_moves_the_memory():
    """Otherwise a token that was WATCH for an hour would notify on its first
    BUY and then never again after dropping back."""
    gate = RobinhoodEntryGate()
    gate.allow(ADDR, "AVOID")
    assert gate.allow(ADDR, "BUY") is True


# --------------------------------------------------------------------------- #
# What the lock screen actually says
# --------------------------------------------------------------------------- #


def test_an_entry_notice_leads_with_why_and_never_claims_a_probability():
    notice = entry_notice(buyable())
    assert notice.action == "BUY"
    assert notice.symbol == "FRONG"
    assert notice.lines, "une notification sans raison n'apprend rien"
    body = " ".join(notice.lines)
    assert "pas une probabilité" in body
    assert "%" not in body.replace(" %", "")


def test_an_entry_notice_carries_the_one_figure_that_decides_exitability():
    body = " ".join(entry_notice(buyable()).lines)
    assert "Liquidité" in body


def test_an_entry_notice_is_french():
    notice = entry_notice(buyable())
    body = " ".join([notice.symbol, *notice.lines])
    for english in (" the ", " is ", "Buy", "Liquidity", "Safety", "probability"):
        assert english not in body, english


def test_a_token_with_no_symbol_shows_a_shortened_address_not_a_blank():
    c = buyable()
    c.symbol = None
    assert entry_notice(c).symbol.startswith("0x")
    assert "…" in entry_notice(c).symbol


def test_an_exit_notice_leads_with_what_broke_not_with_the_gain():
    """Someone woken by this needs to know what broke. Seeing the gain first is
    how a decision gets made on the wrong fact."""
    from cryptopulse.trading.robinhood_decision import RobinhoodTradeDecision

    decision = RobinhoodTradeDecision(
        address=ADDR, symbol="FRONG", action=TradeAction.SELL, strength=None,
        timestamp_ms=NOW, price_usd=0.0002,
        blocking=["Liquidité retirée : 9 000 $ contre 34 000 $ à l'achat"],
    )
    notice = position_notice(
        {"symbol": "FRONG", "pnl_pct": 240.0}, decision, None, None
    )
    assert notice.action == "SELL"
    assert "Liquidité retirée" in notice.lines[0]
    assert notice.pnl is not None and "240" in notice.pnl


def test_an_exit_notice_says_when_health_could_not_be_computed():
    """"We could not tell" must not render as a low score on a lock screen."""
    from cryptopulse.trading.robinhood_decision import RobinhoodTradeDecision
    from cryptopulse.trading.robinhood_health import RobinhoodHealth

    decision = RobinhoodTradeDecision(
        address=ADDR, symbol="FRONG", action=TradeAction.REDUCE, strength=None,
        timestamp_ms=NOW, price_usd=0.0003, risks=["pool en recul"],
    )
    notice = position_notice(
        {"symbol": "FRONG"}, decision,
        RobinhoodHealth(score=None, coverage=0.2), None,
    )
    assert any("non calculable" in line for line in notice.lines)
    assert notice.health is None


# --------------------------------------------------------------------------- #
# The watcher's side
# --------------------------------------------------------------------------- #


async def test_the_watcher_notifies_an_exit_and_a_failure_costs_only_that(monkeypatch):
    """A notification failure must never stop the cycle, for the same reason a
    webhook failure costs a notification and not a scan (invariant 18)."""
    from cryptopulse.database import repo
    from cryptopulse.trading.robinhood_watcher import RobinhoodPositionWatcher

    row = {
        "id": 1, "symbol": "FRONG", "chain": "ROBINHOOD", "contract_address": ADDR,
        "invalidation_price": 0.0003, "entry_price": 0.0004,
        "entry_liquidity_usd": 60_000.0, "entry_early": 80.0, "entry_rug_risk": "LOW",
    }
    monkeypatch.setattr(repo, "positions", lambda status, limit: [row])
    monkeypatch.setattr(repo, "update_position", lambda *a, **k: None)
    monkeypatch.setattr(repo, "record_position_decision", lambda *a, **k: None)

    class Gecko:
        async def token_pools(self, address, page=1):
            pool = PoolSnapshot(
                pool_address="0xp", name="FRONG / WETH", dex="uniswap-v4",
                base=TokenRef(address=address, symbol="FRONG"),
                quote=TokenRef(address="0x" + "ee" * 20, symbol="WETH"),
                created_at_ms=None,
                # Through the invalidation, and the pool has been drained.
                base_price_usd=0.00015, liquidity_usd=4_000.0,
            )
            pool.volume_usd = {"h1": 900.0}
            pool.buys, pool.sells = {"h1": 3}, {"h1": 90}
            return [pool]

    class GoPlus:
        async def token_security(self, addresses, chain_id):
            return {}

    seen: list = []

    async def on_exit(notice):
        seen.append(notice)

    watcher = RobinhoodPositionWatcher(
        SETTINGS, gecko=Gecko(), goplus=GoPlus(), on_exit=on_exit
    )
    report = await watcher.run_once()
    assert report.checked == 1
    assert seen, "une sortie doit être notifiée"
    assert seen[0].action in ("SELL", "REDUCE")

    # Now the same cycle with a channel that throws.
    async def broken(notice):
        raise RuntimeError("Termux:API absent")

    watcher2 = RobinhoodPositionWatcher(
        SETTINGS, gecko=Gecko(), goplus=GoPlus(), on_exit=broken
    )
    report2 = await watcher2.run_once()
    assert report2.checked == 1, "l'échec d'une notification ne doit pas coûter le cycle"


async def test_a_healthy_position_is_never_notified(monkeypatch):
    from cryptopulse.database import repo
    from cryptopulse.trading.robinhood_watcher import RobinhoodPositionWatcher

    row = {
        "id": 1, "symbol": "FRONG", "chain": "ROBINHOOD", "contract_address": ADDR,
        "invalidation_price": 0.0001, "entry_price": 0.0004,
        "entry_liquidity_usd": 60_000.0, "entry_early": 80.0, "entry_rug_risk": "LOW",
    }
    monkeypatch.setattr(repo, "positions", lambda status, limit: [row])
    monkeypatch.setattr(repo, "update_position", lambda *a, **k: None)
    monkeypatch.setattr(repo, "record_position_decision", lambda *a, **k: None)

    class Gecko:
        async def token_pools(self, address, page=1):
            pool = PoolSnapshot(
                pool_address="0xp", name="FRONG / WETH", dex="uniswap-v4",
                base=TokenRef(address=address, symbol="FRONG"),
                quote=TokenRef(address="0x" + "ee" * 20, symbol="WETH"),
                created_at_ms=None, base_price_usd=0.0006, liquidity_usd=58_000.0,
            )
            pool.volume_usd = {"h1": 30_000.0}
            pool.buys, pool.sells = {"h1": 80}, {"h1": 30}
            return [pool]

    class GoPlus:
        async def token_security(self, addresses, chain_id):
            return {}

    seen: list = []

    async def on_exit(notice):
        seen.append(notice)

    watcher = RobinhoodPositionWatcher(
        SETTINGS, gecko=Gecko(), goplus=GoPlus(), on_exit=on_exit
    )
    await watcher.run_once()
    assert seen == [], "une position saine ne doit jamais faire vibrer le téléphone"


# --------------------------------------------------------------------------- #
# The three exit words must never be confused on a lock screen
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "action,emoji,word",
    [("BUY", "🟢", "ACHETER"), ("SELL", "🔴", "VENDRE"), ("REDUCE", "🟠", "RÉDUIRE")],
)
def test_each_decision_keeps_its_own_icon_and_word_on_the_lock_screen(action, emoji, word):
    """Found by running it: `_decision_args` branched on `action == "SELL"`
    alone, so a 🟠 RÉDUIRE rendered as "🔥 🟢 ACHETER" — telling someone to buy
    at exactly the moment the engine wanted them to take profit off the table
    (invariant 48)."""
    from cryptopulse.alerts.notify import DecisionNotice, _decision_args

    args = _decision_args(
        "termux-notification",
        DecisionNotice(action=action, symbol="FRONG", price="0,00042 $", lines=["raison"]),
        synthetic=False,
    )
    title = args[args.index("--title") + 1]
    assert emoji in title and word in title


def test_the_three_decisions_vibrate_differently():
    """They must be distinguishable without looking at the phone."""
    from cryptopulse.alerts.notify import DecisionNotice, _decision_args

    patterns = set()
    for action in ("BUY", "SELL", "REDUCE"):
        args = _decision_args(
            "termux-notification",
            DecisionNotice(action=action, symbol="X", price="1 $"),
            synthetic=False,
        )
        patterns.add(args[args.index("--vibrate") + 1])
    assert len(patterns) == 3


def test_an_entry_notification_keeps_the_two_figures_that_decide_exitability():
    """Found by running it: the body was cut at three lines, which dropped the
    pool depth and the rug verdict — the two things that decide whether the
    position could be closed at all."""
    from cryptopulse.alerts.notify import _decision_args

    args = _decision_args("termux-notification", entry_notice(buyable()), synthetic=False)
    content = args[args.index("--content") + 1]
    assert "Liquidité" in content
    assert "RUG RISK" in content.upper()


def test_a_reduce_and_a_sell_share_one_notification_card():
    """Two exit cards stacked for one position is how a user learns to swipe
    the group away without reading either."""
    from cryptopulse.alerts.notify import DECISION_NOTIFICATION_IDS

    assert DECISION_NOTIFICATION_IDS["REDUCE"] == DECISION_NOTIFICATION_IDS["SELL"]
    assert DECISION_NOTIFICATION_IDS["BUY"] != DECISION_NOTIFICATION_IDS["SELL"]
