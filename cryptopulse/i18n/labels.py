"""French labels for the enums the interface displays.

KEYED BY PLAIN STRINGS, DEPENDING ON NOTHING

The first version keyed these on the enum members themselves, which meant
importing `alerts.engine`, `scoring.states` and `trading.decision` — each of
which wants to use the labels, producing an import cycle that only showed up at
runtime. A translation table that depends on the code it labels is a table that
cannot be used from the code it labels.

WHY THE ENUM VALUES THEMSELVES STAY ENGLISH

`SetupState.BREAKOUT` is written into SQLite, filtered on by the API, compared
in `trading/decision.py` and stored in every historical signal row. Translating
the *value* would be a database migration wearing a translation's clothes: old
rows would say BREAKOUT, new ones CASSURE, and every query would have to know
both. The user asked for the interface to be French, and explicitly allowed
internal technical names to stay English (spec §1).

So the value is the identifier and the label is the French text, and the API
sends both. The interface renders the label and never the value — a test pins
that, because a component that falls back to the raw value would print
"BREAKOUT" on screen and nobody would notice until it shipped.
"""

from __future__ import annotations

__all__ = ["SETUP_STATE_FR", "SETUP_STATE_LONG_FR", "ALERT_LEVEL_FR", "LIQUIDITY_FR",
           "TRADE_ACTION_FR", "STRENGTH_FR", "REGIME_FR", "label_for"]

# Short label — what fits on a phone card.
SETUP_STATE_FR: dict[str, str] = {
    "IGNORE": "IGNORER",
    "OBSERVE": "OBSERVER",
    "WATCH": "SURVEILLER",
    "ARMED": "PRÊT",
    "BREAKOUT": "CASSURE",
    "RETEST": "RETEST",
    "CONTINUATION": "CONTINUATION",
    "INVALIDATED": "INVALIDÉ",
}

# Long label — the sentence under the badge, when there is room for it.
SETUP_STATE_LONG_FR: dict[str, str] = {
    "IGNORE": "IGNORER",
    "OBSERVE": "OBSERVER",
    "WATCH": "SURVEILLER",
    "ARMED": "PRÊT — EN ATTENTE DU DÉCLENCHEMENT",
    "BREAKOUT": "CASSURE CONFIRMÉE",
    "RETEST": "RETEST DU NIVEAU CASSÉ",
    "CONTINUATION": "CONTINUATION DE LA HAUSSE",
    "INVALIDATED": "FAUSSE CASSURE — SETUP INVALIDÉ",
}

ALERT_LEVEL_FR: dict[str, str] = {
    "INFO": "INFORMATION",
    "WATCH": "SURVEILLER",
    "HIGH": "ÉLEVÉ",
    "CRITICAL_SETUP": "CRITIQUE",
}

LIQUIDITY_FR: dict[str, str] = {
    "EXCELLENT": "EXCELLENTE",
    "GOOD": "BONNE",
    "ACCEPTABLE": "ACCEPTABLE",
    "POOR": "FAIBLE",
    "DANGEROUS": "DANGEREUSE",
    "UNKNOWN": "INCONNUE",
}

# The six decisions, exactly as specified (spec §4/§21). Green is only ever a
# new entry and blue only ever an open position; they never share a colour.
TRADE_ACTION_FR: dict[str, str] = {
    "BUY": "ACHETER",
    "HOLD": "CONSERVER",
    "WATCH": "SURVEILLER",
    "REDUCE": "RÉDUIRE / PROTÉGER",
    "SELL": "VENDRE",
    "AVOID": "NE PAS ACHETER",
}

STRENGTH_FR: dict[str, str] = {
    "LOW": "FAIBLE",
    "MEDIUM": "MOYEN",
    "HIGH": "ÉLEVÉ",
    "VERY_HIGH": "TRÈS ÉLEVÉ",
    "STRONG": "FORT",
    "VERY_STRONG": "TRÈS FORT",
    "CRITICAL": "CRITIQUE",
    "WEAK": "FAIBLE",
}

REGIME_FR: dict[str, str] = {
    "BULL_TREND": "TENDANCE HAUSSIÈRE",
    "BEAR_TREND": "TENDANCE BAISSIÈRE",
    "RANGE": "MARCHÉ SANS TENDANCE",
    "UPTREND": "HAUSSIER",
    "DOWNTREND": "BAISSIER",
    "NEUTRAL": "NEUTRE",
    "UNKNOWN": "INCONNU",
    "HIGH_VOLATILITY": "VOLATILITÉ ÉLEVÉE",
    "LOW_VOLATILITY": "VOLATILITÉ FAIBLE",
    "NORMAL": "NORMALE",
}


def label_for(table: dict, key, fallback: str | None = None) -> str:
    """Look a label up without ever silently printing the English value.

    The fallback is a French placeholder, not `str(key)`: a missing entry must
    look like a missing entry, not like a technical identifier that happens to
    be readable.
    """
    if key in table:
        return table[key]
    value = getattr(key, "value", key)
    if isinstance(value, str) and value in table:
        return table[value]
    return fallback or "—"
