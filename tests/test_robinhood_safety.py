"""ROBINHOOD_SAFETY_V1 + RUG_RISK, and the veto that outranks every score.

The payloads follow GoPlus's own Swagger schema, read from the `goplus` 0.2.5
package on PyPI — every field a string, absences meaningful. The host is refused
by this environment, so nothing here proves the live API; `doctor-robinhood` on
a machine with egress does that.

The failure this file mostly exists to prevent: a missing check being read as a
pass. On a token minted four minutes ago, "we did not look" and "it is clean"
must never render the same.
"""

from __future__ import annotations

import pytest

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.providers.goplus import parse_token_security
from cryptopulse.risk.robinhood_safety import (
    SAFETY_WEIGHTS,
    RugRisk,
    Severity,
    assess_safety,
    safety_fingerprint,
    unanalysed,
)

SETTINGS = RobinhoodSettings()


def clean_raw(**overrides) -> dict:
    """A token with every check present and every answer benign."""
    raw = {
        "is_honeypot": "0", "cannot_sell_all": "0", "cannot_buy": "0",
        "buy_tax": "0", "sell_tax": "0",
        "is_mintable": "0", "owner_change_balance": "0",
        "can_take_back_ownership": "0", "hidden_owner": "0", "selfdestruct": "0",
        "is_proxy": "0", "is_blacklisted": "0", "transfer_pausable": "0",
        "is_open_source": "1", "slippage_modifiable": "0",
        "owner_percent": "0", "creator_percent": "0.02",
        "holder_count": "1420", "lp_holder_count": "3",
        "token_name": "Xyz", "token_symbol": "XYZ",
        "holders": [{"address": "0xa", "percent": "0.05", "is_locked": "0", "is_contract": "0"}],
        "lp_holders": [
            {"address": "0x000000000000000000000000000000000000dead",
             "percent": "0.92", "is_locked": "0", "is_contract": "0"},
        ],
    }
    raw.update(overrides)
    return raw


def sec(**overrides):
    return parse_token_security("0xabc", clean_raw(**overrides))


def report(**overrides):
    return assess_safety(sec(**overrides), SETTINGS)


# --------------------------------------------------------------------------- #
# Tri-state parsing: the rule the whole module rests on
# --------------------------------------------------------------------------- #


def test_absent_is_none_never_false():
    """GoPlus omits checks it did not run. Reading a missing is_honeypot as
    'not a honeypot' turns 'we do not know' into 'it is safe'."""
    s = parse_token_security("0xabc", {"is_open_source": "1"})
    assert s.is_honeypot is None
    assert s.cannot_sell_all is None
    assert s.is_mintable is None
    assert s.is_honeypot is not False


def test_string_flags_are_decoded_both_ways():
    assert sec(is_honeypot="1").is_honeypot is True
    assert sec(is_honeypot="0").is_honeypot is False
    assert sec(is_honeypot="unexpected").is_honeypot is None


def test_coverage_counts_the_checks_that_came_back():
    full = sec()
    thin = parse_token_security("0xabc", {"is_honeypot": "0"})
    assert full.coverage > 0.9
    assert thin.coverage < 0.1
    assert thin.checks_present == 1


def test_lp_burned_to_dead_counts_as_locked():
    """LP sent to a burn address can never be pulled — that is the strongest
    form of lock there is, and it is not flagged is_locked."""
    assert sec().lp_locked_percent == pytest.approx(92.0)


def test_shares_are_normalised_to_percent_exactly_once():
    """GoPlus returns fractions ("0.92" = 92 %). Comparing those against
    thresholds in percent broke both directions: burned liquidity read as 0.92
    and looked unlocked, while ten holders at 9 % summed to 0.9 and never
    tripped a 60 % limit."""
    s = sec(holders=[{"address": "0xa", "percent": "0.32", "is_locked": "0", "is_contract": "0"}],
            creator_percent="0.25")
    assert s.holders[0].percent == pytest.approx(32.0)
    assert s.creator_percent == pytest.approx(25.0)
    # Already-percent values are passed through rather than doubled.
    s2 = sec(creator_percent="42")
    assert s2.creator_percent == pytest.approx(42.0)


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #


def test_a_clean_token_scores_high_with_low_rug_risk():
    r = report()
    assert r.score is not None and r.score > 85
    assert r.rug_risk is RugRisk.LOW
    assert r.hard_veto is False
    assert r.blocking_findings == []


def test_a_honeypot_is_blocking_whatever_else_is_true():
    """Spec §19: even with everything else perfect."""
    r = report(is_honeypot="1")
    assert r.rug_risk is RugRisk.CRITICAL
    assert r.hard_veto is True
    assert any(f.code == "HONEYPOT" and f.blocking for f in r.findings)
    assert r.score == 0.0, "a veto zeroes rather than reduces (invariant 40)"


@pytest.mark.parametrize("field_name", [
    "cannot_sell_all", "cannot_buy", "owner_change_balance",
    "hidden_owner", "selfdestruct", "transfer_pausable",
    "personal_slippage_modifiable", "honeypot_with_same_creator",
    "is_airdrop_scam",
])
def test_every_critical_condition_forces_a_veto(field_name):
    """The list in spec §19, each one on its own, with the rest clean."""
    r = report(**{field_name: "1"})
    assert r.hard_veto is True, field_name
    assert r.rug_risk is RugRisk.CRITICAL, field_name


def test_the_veto_survives_a_perfect_token_otherwise():
    """A trap with flawless liquidity, distribution and taxes is still a trap."""
    r = report(is_honeypot="1", lp_holders=[
        {"address": "0x000000000000000000000000000000000000dead", "percent": "1.0",
         "is_locked": "1", "is_contract": "0"}])
    assert r.hard_veto is True


def test_unknown_coverage_is_unknown_not_bad():
    """A thin analysis forbids a BUY without claiming the token is dangerous —
    two different statements, and the UI shows which one it is."""
    r = assess_safety(parse_token_security("0xabc", {"is_honeypot": "0"}), SETTINGS)
    assert r.rug_risk is RugRisk.UNKNOWN
    assert r.score is None, "an unexamined token must not sort next to a proven honeypot"
    assert r.hard_veto is True
    assert any(f.code == "THIN_COVERAGE" for f in r.findings)


def test_a_token_the_source_never_saw_is_unanalysed_not_zero():
    r = unanalysed("0xdead", SETTINGS, reason="absent de la réponse")
    assert r.score is None
    assert r.analysed is False
    assert r.rug_risk is RugRisk.UNKNOWN
    assert r.hard_veto is True


def test_an_unknown_honeypot_check_costs_points_and_says_so():
    """Not blocking on its own — coverage handles that — but never free."""
    raw = clean_raw()
    del raw["is_honeypot"]
    r = assess_safety(parse_token_security("0xabc", raw), SETTINGS)
    assert any(f.code == "HONEYPOT_UNKNOWN" and f.severity is Severity.UNKNOWN for f in r.findings)
    assert r.score < report().score


def test_a_veto_zeroes_the_score_rather_than_reducing_it():
    """Found by running the scorer: a proven honeypot came out at 70/100
    because only the sellability group was lost, and 70 reads as 'fine' at a
    glance. Same rule as the explosion engine's hard gate (invariant 40)."""
    r = report(is_honeypot="1")
    assert r.score == 0.0
    assert r.blocking_findings, "the zero must always carry its reason"


def test_rug_risk_high_blocks_on_the_level_alone():
    """Spec §19 lists RUG RISK HIGH beside CRITICAL. Found by running the
    scorer: three independent owner powers produced HIGH and no veto."""
    r = report(is_mintable="1", is_proxy="1", is_blacklisted="1")
    assert r.rug_risk is RugRisk.HIGH
    assert r.hard_veto is True


def test_modifiable_taxes_are_worse_than_high_taxes():
    """A 3% tax that can become 100% tomorrow is the trap; a fixed 12% is
    merely expensive."""
    modifiable = report(buy_tax="0.03", slippage_modifiable="1")
    fixed_high = report(buy_tax="0.12")
    assert modifiable.score < fixed_high.score
    assert not modifiable.blocking_findings, "this test must compare scores, not vetoes"


def test_a_hundred_percent_tax_blocks():
    r = report(sell_tax="1.0")
    assert r.hard_veto is True
    assert any(f.code == "SELL_TAX_TOTAL" for f in r.findings)


def test_unlocked_liquidity_is_penalised_by_how_unlocked_it_is():
    barely = report(lp_holders=[{"address": "0xdev", "percent": "0.05", "is_locked": "0", "is_contract": "0"}])
    partly = report(lp_holders=[
        {"address": "0x000000000000000000000000000000000000dead", "percent": "0.35",
         "is_locked": "0", "is_contract": "0"},
        {"address": "0xdev", "percent": "0.65", "is_locked": "0", "is_contract": "0"},
    ])
    assert barely.score < partly.score
    assert any(f.code == "LP_UNLOCKED" and f.severity is Severity.CRITICAL for f in barely.findings)


def test_concentration_is_flagged():
    r = report(holders=[
        {"address": f"0x{i}", "percent": "0.09", "is_locked": "0", "is_contract": "0"}
        for i in range(10)
    ])
    assert any(f.code == "TOP10_CONCENTRATED" for f in r.findings)


# --------------------------------------------------------------------------- #
# Structure
# --------------------------------------------------------------------------- #


def test_findings_are_counted_by_severity_and_there_is_no_total():
    """Same rule as exit_risk: five cosmetic warnings must not outweigh one
    honeypot, and a total would let them."""
    r = report(trading_cooldown="1", external_call="1", is_mintable="1")
    counts = r.counts()
    assert set(counts) == {"CRITICAL", "MAJOR", "MINOR", "UNKNOWN"}
    assert "total" not in counts
    assert not hasattr(r, "total")


def test_rug_risk_escalates_on_the_number_of_majors_not_their_sum():
    one = report(is_mintable="1")
    three = report(is_mintable="1", is_proxy="1", is_blacklisted="1")
    assert one.rug_risk is RugRisk.MEDIUM
    assert three.rug_risk is RugRisk.HIGH


def test_one_critical_outranks_any_number_of_minors():
    r = report(trading_cooldown="1", external_call="1", selfdestruct="1")
    assert r.rug_risk is RugRisk.CRITICAL


def test_every_finding_is_named_in_french():
    r = report(is_honeypot="1", is_mintable="1")
    for f in r.findings:
        assert f.label_fr and f.label_fr.strip(), f.code
        assert f.code.isupper()


def test_unknown_and_low_never_share_a_presentation():
    """'no risk found' and 'no analysis' must not render the same."""
    from cryptopulse.risk.robinhood_safety import RUG_PRESENTATION

    assert RUG_PRESENTATION[RugRisk.UNKNOWN] != RUG_PRESENTATION[RugRisk.LOW]
    assert RUG_PRESENTATION[RugRisk.UNKNOWN][0] != RUG_PRESENTATION[RugRisk.LOW][0]


def test_weights_sum_to_one_hundred():
    assert sum(SAFETY_WEIGHTS.values()) == pytest.approx(100.0)


def test_the_fingerprint_moves_when_a_threshold_moves():
    """A changed threshold is a changed claim, so past verdicts are never
    reinterpreted under new rules."""
    a = safety_fingerprint(RobinhoodSettings())
    b = safety_fingerprint(RobinhoodSettings(safety_max_sell_tax=25.0))
    assert a != b


def test_the_report_serialises_its_whole_story():
    d = report(is_honeypot="1").to_dict()
    assert d["rug_risk"] == "CRITICAL"
    assert d["rug_emoji"] == "🔴"
    assert d["hard_veto"] is True
    assert d["blocking"]
    assert d["engine_version"] == "ROBINHOOD_SAFETY_V1"
    assert d["weights_fingerprint"]
    assert d["score_label"].endswith("/100")


def test_an_unanalysed_report_says_non_disponible_rather_than_a_number():
    d = unanalysed("0xabc", SETTINGS, reason="x").to_dict()
    assert d["score"] is None
    assert d["score_label"] == "NON DISPONIBLE"


def test_every_veto_carries_at_least_one_reason():
    """Found by running the UI: a token vetoed for HIGH rug risk had an empty
    `blocking` list, so the screen fell back to 'Sécurité non analysée' on a
    token that had been fully analysed. The UI has no fallback now, so this
    guarantee is what stands between it and a blank reason."""
    cases = [
        report(is_honeypot="1"),
        report(is_mintable="1", is_proxy="1", is_blacklisted="1"),   # HIGH by level
        unanalysed("0xabc", SETTINGS, reason="x"),
        assess_safety(parse_token_security("0xabc", {"is_honeypot": "0"}), SETTINGS),
    ]
    for r in cases:
        assert r.hard_veto is True
        assert r.blocking_findings, f"{r.rug_risk} vetoed with no stated reason"
        assert all(f.label_fr.strip() for f in r.blocking_findings)


def test_a_token_without_a_veto_has_no_blocking_findings():
    r = report()
    assert r.hard_veto is False
    assert r.blocking_findings == []
