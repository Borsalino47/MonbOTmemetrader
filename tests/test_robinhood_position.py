"""Holding a token on Robinhood Chain: health, exit risk, and the watcher.

THE FAILURE THIS FILE EXISTS TO PREVENT

A DEX position does not go to zero the way a listed pair does. It goes to zero
because the contract's owner did something after you bought, or because whoever
provided the liquidity took it back. Both are invisible to anything that only
watches price, and both are checked here.

The second failure, equally expensive in the other direction: telling someone to
sell because an indexer request failed. An unknown health is not a bad one, and
a token the indexer stopped listing this cycle is not a token to dump.
"""

from __future__ import annotations

import pytest

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.hunter.robinhood_discovery import TokenCandidate
from cryptopulse.providers.geckoterminal import PoolSnapshot, TokenRef
from cryptopulse.risk.robinhood_safety import (
    RugRisk,
    SafetyFinding,
    SafetyReport,
    Severity,
)
from cryptopulse.scoring.robinhood_early import EarlyScore
from cryptopulse.trading.decision import TradeAction
from cryptopulse.trading.robinhood_decision import RobinhoodTradeDecisionEngine
from cryptopulse.trading.robinhood_exit_risk import assess_robinhood_exit_risk
from cryptopulse.trading.robinhood_health import (
    RH_HEALTH_WEIGHTS,
    compute_robinhood_health,
)

SETTINGS = RobinhoodSettings()
ADDR = "0x" + "ab" * 20
NOW = 1_700_000_000_000


class Entry:
    """The state a position row records at entry, as the engines read it."""

    def __init__(self, **kw):
        self.symbol = kw.get("symbol", "XYZ")
        self.entry_price = kw.get("entry_price", 0.0004)
        self.invalidation_price = kw.get("invalidation_price", 0.0003)
        self.trigger_price = kw.get("trigger_price", None)
        self.entry_early = kw.get("entry_early", 80.0)
        self.entry_liquidity_usd = kw.get("entry_liquidity_usd", 60_000.0)
        self.entry_rug_risk = kw.get("entry_rug_risk", "LOW")
        self.entry_safety = kw.get("entry_safety", 95.0)


def safety(score=95.0, rug=RugRisk.LOW, findings=None, analysed=True) -> SafetyReport:
    return SafetyReport(
        address=ADDR, score=score, rug_risk=rug, analysed=analysed,
        coverage=0.9, findings=findings or [],
    )


def honeypot() -> SafetyReport:
    return SafetyReport(
        address=ADDR, score=0.0, rug_risk=RugRisk.CRITICAL, analysed=True, coverage=0.9,
        findings=[SafetyFinding("HONEYPOT", Severity.CRITICAL,
                                "HONEYPOT — revente impossible", blocking=True)],
    )


def held(
    *, price=0.0005, liquidity=58_000.0, buys=60, sells=30, early=82.0,
    safety_report=None, volume_h1=25_000.0,
) -> TokenCandidate:
    c = TokenCandidate(address=ADDR, symbol="XYZ", name="Xyz")
    pool = PoolSnapshot(
        pool_address="0xpool", name="XYZ / WETH", dex="uniswap-v4",
        base=TokenRef(address=ADDR, symbol="XYZ"),
        quote=TokenRef(address="0x" + "ee" * 20, symbol="WETH"),
        created_at_ms=None, base_price_usd=price, liquidity_usd=liquidity,
    )
    pool.volume_usd = {"h1": volume_h1, "m5": volume_h1 / 12}
    pool.buys, pool.sells = {"h1": buys}, {"h1": sells}
    c.pools = [pool]
    c.price_usd = price
    c.liquidity_usd = liquidity
    c.safety = safety() if safety_report is None else safety_report
    c.early = None if early is None else EarlyScore(score=early)
    return c


@pytest.fixture
def engine() -> RobinhoodTradeDecisionEngine:
    return RobinhoodTradeDecisionEngine(SETTINGS)


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


def test_health_weights_sum_to_one_hundred():
    assert sum(RH_HEALTH_WEIGHTS.values()) == pytest.approx(100.0)


def test_a_thesis_still_standing_scores_healthy():
    h = compute_robinhood_health(Entry(), held())
    assert h.score is not None and h.score >= 70.0
    assert h.label_fr == "SAINE"


def test_health_is_not_pnl():
    """Invariant 49: a position up 300 % on a pool that lost most of its depth
    is unhealthy, and saying so is the point — that is the moment the gain
    becomes impossible to realise."""
    rich_but_rotten = held(price=0.0016, liquidity=6_000.0)   # +300 %, pool -90 %
    h = compute_robinhood_health(Entry(), rich_but_rotten)
    assert h.score is not None and h.score < 70.0
    assert any("profondeur" in c.lower() or "pool" in c.lower() for c in h.caveats())


def test_an_unknown_health_is_none_rather_than_low():
    """Invariant 50. Below half coverage the answer is "we cannot tell"."""
    bare = TokenCandidate(address=ADDR, symbol=None, name=None)
    bare.price_usd = None
    h = compute_robinhood_health(Entry(invalidation_price=None), bare)
    assert h.score is None
    assert h.label_fr == "INCONNUE"


def test_a_missing_component_never_silently_lowers_the_score():
    """The scale is rescaled over the weight actually measured, so an absent
    input costs coverage rather than points."""
    full = compute_robinhood_health(Entry(), held())
    no_flow = held()
    no_flow.pools[0].buys, no_flow.pools[0].sells = {}, {}
    partial = compute_robinhood_health(Entry(), no_flow)
    assert partial.coverage < full.coverage
    assert partial.score is not None and partial.score >= 70.0


def test_a_honeypot_appearing_after_entry_destroys_sellability():
    h = compute_robinhood_health(Entry(), held(safety_report=honeypot()))
    sell = next(c for c in h.components if c.name == "sellability")
    assert sell.points == 0.0
    assert any("HONEYPOT" in c for c in sell.caveats)


def test_health_says_which_reading_it_could_not_take():
    h = compute_robinhood_health(Entry(), held(safety_report=safety(analysed=False)))
    assert any("indisponible" in c.lower() for c in h.caveats())


# --------------------------------------------------------------------------- #
# Exit risk
# --------------------------------------------------------------------------- #


def test_the_report_has_no_total():
    """Invariant 52: summing would let five cosmetic warnings outweigh one
    withdrawn pool."""
    payload = assess_robinhood_exit_risk(Entry(), held()).to_dict()
    assert "total" not in payload
    assert "score" not in payload
    assert set(payload["counts"]) == {"critical", "major", "minor"}


def test_a_broken_invalidation_is_critical_whatever_else_is_true():
    """Invariant 51: it was agreed before the position existed."""
    report = assess_robinhood_exit_risk(Entry(), held(price=0.0002))
    assert report.invalidation_broken
    assert report.critical


def test_liquidity_withdrawn_is_critical():
    report = assess_robinhood_exit_risk(Entry(entry_liquidity_usd=60_000.0),
                                        held(liquidity=9_000.0))
    assert any(s.name == "LIQUIDITY_PULLED" for s in report.critical)


def test_liquidity_draining_is_major_not_critical():
    report = assess_robinhood_exit_risk(Entry(entry_liquidity_usd=60_000.0),
                                        held(liquidity=30_000.0))
    assert any(s.name == "LIQUIDITY_DRAINING" for s in report.major)
    assert not report.critical


def test_a_contract_that_turned_hostile_after_entry_is_critical():
    report = assess_robinhood_exit_risk(Entry(), held(safety_report=honeypot()))
    assert any(s.name.startswith("SAFETY_") for s in report.critical)


def test_a_rug_level_that_worsened_since_entry_is_its_own_signal():
    """A level that was always mediocre is a risk taken knowingly. A level that
    got worse is something that changed under the position."""
    report = assess_robinhood_exit_risk(
        Entry(entry_rug_risk="LOW"), held(safety_report=safety(score=60.0, rug=RugRisk.HIGH))
    )
    assert any(s.name == "RUG_WORSENED" for s in report.signals)


def test_becoming_unanalysable_counts_as_worse_not_as_unchanged():
    """UNKNOWN ranks below every level: we can no longer tell whether the exit
    still exists, and that is a deterioration, not a neutral event."""
    report = assess_robinhood_exit_risk(
        Entry(entry_rug_risk="LOW"),
        held(safety_report=safety(score=None, rug=RugRisk.UNKNOWN)),
    )
    assert any(s.name == "RUG_WORSENED" for s in report.signals)


def test_a_market_that_went_quiet_is_named():
    report = assess_robinhood_exit_risk(Entry(), held(volume_h1=120.0))
    assert any(s.name == "VOLUME_GONE" for s in report.major)


def test_what_could_not_be_read_is_listed_rather_than_scored():
    bare = TokenCandidate(address=ADDR, symbol=None, name=None)
    report = assess_robinhood_exit_risk(Entry(invalidation_price=None), bare)
    assert "invalidation" in report.unavailable
    assert "safety" in report.unavailable
    assert not report.critical, "une donnée absente ne produit jamais un signal critique"


def test_messages_come_out_worst_first():
    report = assess_robinhood_exit_risk(
        Entry(), held(price=0.0002, liquidity=5_000.0, buys=5, sells=90)
    )
    assert report.messages(1)[0] == report.critical[0].message


# --------------------------------------------------------------------------- #
# The decision about something held
# --------------------------------------------------------------------------- #


def _decide(engine, entry, candidate):
    health = compute_robinhood_health(entry, candidate)
    risk = assess_robinhood_exit_risk(entry, candidate)
    return engine.decide_position(entry, candidate, health, risk, now_ms=NOW)


def test_a_healthy_position_is_conserved(engine):
    d = _decide(engine, Entry(), held())
    assert d.action is TradeAction.HOLD
    assert d.emoji == "🔵"


def test_acheter_is_never_offered_for_something_already_held(engine):
    """Recommending a purchase of something owned is a different instruction —
    adding to a position — which this product does not model."""
    for candidate in (held(), held(price=0.0002), held(safety_report=honeypot())):
        assert _decide(engine, Entry(), candidate).action is not TradeAction.BUY


def test_a_broken_invalidation_sells_whatever_the_flow_says(engine):
    d = _decide(engine, Entry(), held(price=0.0002, buys=500, sells=1))
    assert d.action is TradeAction.SELL
    assert any("INVALIDÉ" in b for b in d.blocking)


def test_a_rug_in_progress_sells(engine):
    d = _decide(engine, Entry(), held(liquidity=4_000.0))
    assert d.action is TradeAction.SELL


def test_an_unknown_health_watches_it_never_sells(engine):
    """Invariant 50, and the reason it matters more here: on a chain whose
    indexers are young, a failed request is ordinary."""
    bare = TokenCandidate(address=ADDR, symbol="XYZ", name=None)
    bare.price_usd = None
    d = _decide(engine, Entry(invalidation_price=None), bare)
    assert d.action is TradeAction.WATCH
    assert d.action is not TradeAction.SELL


def test_a_degrading_position_is_reduced_before_it_is_sold(engine):
    d = _decide(engine, Entry(entry_liquidity_usd=60_000.0),
                held(liquidity=32_000.0, buys=20, sells=45, early=60.0))
    assert d.action in (TradeAction.REDUCE, TradeAction.WATCH, TradeAction.SELL)
    assert d.action is not TradeAction.HOLD


def test_a_held_decision_carries_no_strength(engine):
    """Strength grades a buy signal. A held position is not a signal."""
    assert _decide(engine, Entry(), held()).strength is None


# --------------------------------------------------------------------------- #
# The watcher
# --------------------------------------------------------------------------- #


async def test_the_watcher_costs_nothing_when_nothing_is_held(monkeypatch):
    from cryptopulse.database import repo
    from cryptopulse.trading.robinhood_watcher import RobinhoodPositionWatcher

    monkeypatch.setattr(repo, "positions", lambda status, limit: [])
    watcher = RobinhoodPositionWatcher(SETTINGS, gecko=None, goplus=None)
    report = await watcher.run_once()
    assert report.requests_used == 0
    assert report.checked == 0
    assert any("ne coûte rien" in n for n in report.notes)


async def test_the_watcher_ignores_binance_positions(monkeypatch):
    from cryptopulse.database import repo
    from cryptopulse.trading.robinhood_watcher import RobinhoodPositionWatcher

    monkeypatch.setattr(
        repo, "positions",
        lambda status, limit: [
            {"id": 1, "symbol": "BTCUSDT", "chain": "CEX", "contract_address": None},
        ],
    )
    watcher = RobinhoodPositionWatcher(SETTINGS, gecko=None, goplus=None)
    report = await watcher.run_once()
    assert report.checked == 0 and report.requests_used == 0


async def test_a_token_the_indexer_dropped_is_reported_not_sold(monkeypatch):
    """The pool vanishing from the index is a fact about our data. Inventing a
    SELL from it would be the worst false alarm this loop could produce."""
    from cryptopulse.database import repo
    from cryptopulse.trading.robinhood_watcher import RobinhoodPositionWatcher

    row = {
        "id": 7, "symbol": "XYZ", "chain": "ROBINHOOD", "contract_address": ADDR,
        "invalidation_price": 0.0003, "entry_price": 0.0004,
    }
    monkeypatch.setattr(repo, "positions", lambda status, limit: [row])

    class Gecko:
        async def token_pools(self, address, page=1):
            return []

    watcher = RobinhoodPositionWatcher(SETTINGS, gecko=Gecko(), goplus=None)
    report = await watcher.run_once()
    assert report.checked == 0
    assert "XYZ" in report.errors
    assert report.requests_used == 1, "une requête a bien été dépensée et elle est comptée"


async def test_the_watcher_states_the_requests_it_actually_spent(monkeypatch):
    """Invariant 28. One request per held token for the pools — the indexer has
    no token->pools batch — plus one per batch of thirty for safety."""
    from cryptopulse.database import repo
    from cryptopulse.trading.robinhood_watcher import RobinhoodPositionWatcher

    rows = [
        {"id": i, "symbol": f"T{i}", "chain": "ROBINHOOD",
         "contract_address": "0x" + f"{i:02d}" + "cd" * 19,
         "invalidation_price": 0.0001, "entry_price": 0.0002,
         "entry_liquidity_usd": 50_000.0, "entry_early": 70.0, "entry_rug_risk": "LOW"}
        for i in range(3)
    ]
    monkeypatch.setattr(repo, "positions", lambda status, limit: rows)
    monkeypatch.setattr(repo, "update_position", lambda *a, **k: None)
    monkeypatch.setattr(repo, "record_position_decision", lambda *a, **k: None)

    class Gecko:
        async def token_pools(self, address, page=1):
            pool = PoolSnapshot(
                pool_address="0xp", name="T / WETH", dex="uniswap-v4",
                base=TokenRef(address=address, symbol="T"),
                quote=TokenRef(address="0x" + "ee" * 20, symbol="WETH"),
                created_at_ms=None, base_price_usd=0.0005, liquidity_usd=48_000.0,
            )
            pool.volume_usd = {"h1": 20_000.0}
            pool.buys, pool.sells = {"h1": 40}, {"h1": 20}
            return [pool]

    class GoPlus:
        calls = 0

        async def token_security(self, addresses, chain_id):
            GoPlus.calls += 1
            return {}

    watcher = RobinhoodPositionWatcher(SETTINGS, gecko=Gecko(), goplus=GoPlus())
    report = await watcher.run_once()
    # 3 token-pool calls + 1 batched safety call.
    assert report.requests_used == 4
    assert GoPlus.calls == 1
    assert report.checked == 3


async def test_the_watcher_never_opens_or_closes_a_position():
    """It records and recommends. Opening and closing are the user's acts, and
    this loop must not be able to perform them even by accident."""
    from pathlib import Path

    source = Path("cryptopulse/trading/robinhood_watcher.py").read_text(encoding="utf-8")
    for forbidden in ("open_position", "close_position", "delete_position", "place_order"):
        assert forbidden not in source, forbidden
    # The only writes it is allowed to make.
    assert "repo.update_position" in source
    assert "repo.record_position_decision" in source


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


def test_the_new_position_columns_are_additive_and_nullable():
    """Spec §45: a migration may add, never destroy. Existing Binance rows must
    survive with these simply null."""
    from cryptopulse.database.models import PositionRecord

    columns = PositionRecord.__table__.columns
    for name in ("entry_early", "entry_liquidity_usd", "entry_rug_risk", "chain_id"):
        assert name in columns, name
        assert columns[name].nullable, f"{name} doit être nullable"


# --------------------------------------------------------------------------- #
# A catastrophic reading caps, it does not merely cost points
# --------------------------------------------------------------------------- #


def test_a_collapsed_pool_caps_the_score_rather_than_deducting_from_it():
    """Found by writing the invariant-49 test and watching it fail: a position
    up 300 % whose pool had lost 90 % of its depth scored 73.5 and rendered as
    SAINE, because liquidity is only 25 of the 100 points and everything else
    was perfect. A withdrawn pool is not a quarter of a problem."""
    h = compute_robinhood_health(Entry(entry_liquidity_usd=60_000.0),
                                 held(price=0.0016, liquidity=6_000.0))
    assert h.score is not None and h.score <= 30.0
    assert h.label_fr in ("FRAGILE", "ROMPUE")
    liquidity = next(c for c in h.components if c.name == "liquidity")
    assert liquidity.caps_at is not None


def test_a_honeypot_caps_the_score_however_good_everything_else_is():
    h = compute_robinhood_health(Entry(), held(safety_report=honeypot(), buys=900, sells=1))
    assert h.score is not None and h.score <= 15.0
    assert h.label_fr == "ROMPUE"


def test_the_lowest_cap_wins_and_caps_are_never_averaged():
    """Two catastrophes are not the mean of two catastrophes."""
    h = compute_robinhood_health(
        Entry(entry_liquidity_usd=60_000.0),
        held(price=0.0002, liquidity=5_000.0, safety_report=honeypot()),
    )
    caps = [c.caps_at for c in h.components if c.caps_at is not None]
    assert len(caps) >= 2
    assert h.score is not None and h.score <= min(caps)


def test_a_healthy_position_declares_no_cap():
    h = compute_robinhood_health(Entry(), held())
    assert all(c.caps_at is None for c in h.components)
