"""ROBINHOOD_SAFETY_V1 and RUG_RISK — can this token be bought and sold at all?

This is the only engine here whose job is to say no, and it is the one the
whole Robinhood universe is gated on: a token four minutes old with beautiful
volume is exactly what a honeypot looks like from the outside.

THREE RULES IT IS BUILT ON

1. **Unknown is never safe.** Every GoPlus flag is tri-state, and a missing
   check reduces both the score and the coverage rather than passing. Below
   `safety_min_coverage` the verdict is UNKNOWN, which forbids a BUY (spec §53)
   without claiming the token is bad — those are different statements and the
   UI shows which one it is.

2. **Findings are counted by severity, never summed.** Same rule as
   `trading/exit_risk.py`: five cosmetic warnings must not outweigh one
   honeypot, and a total would let them. `RUG_RISK` is decided by the worst
   findings present and how many, not by adding numbers up.

3. **A hard veto zeroes rather than reduces.** Spec §19: honeypot, cannot sell,
   critical rug risk and friends force ⚫ NE PAS ACHETER even with every other
   score at 100. A merely-reduced score would still let a trap outrank a sound
   token in a sorted list — the exact failure the explosion engine's hard gate
   was written to avoid.

The weights below are a stated hypothesis, like every other set in this project.
They have not been fitted to outcomes, because no Robinhood outcome history
exists yet. Changing one changes the fingerprint, so past verdicts are never
reinterpreted under new rules.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.core.logging import get_logger
from cryptopulse.providers.goplus import TokenSecurity

log = get_logger("risk.robinhood_safety")

__all__ = [
    "SAFETY_ENGINE_VERSION",
    "SAFETY_WEIGHTS",
    "RugRisk",
    "Severity",
    "SafetyFinding",
    "SafetyReport",
    "assess_safety",
    "safety_fingerprint",
]

SAFETY_ENGINE_VERSION = "ROBINHOOD_SAFETY_V1"

# Five groups, 100 points. Sellability dominates because everything else is
# worth nothing if the position cannot be exited.
SAFETY_WEIGHTS: dict[str, float] = {
    "sellability": 30.0,      # honeypot, cannot sell/buy
    "contract_control": 25.0,  # what the owner can still do to you
    "taxes": 15.0,            # what a round trip costs
    "lp_security": 15.0,      # can the floor be removed
    "distribution": 15.0,     # who can dump on you
}


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"
    # Not a finding about the token — a finding about our knowledge of it.
    UNKNOWN = "UNKNOWN"


class RugRisk(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"
    # Distinct from every level above: we could not look. Never rendered in the
    # same colour as LOW, because "no risk found" and "no analysis" are not the
    # same sentence.
    UNKNOWN = "UNKNOWN"


# level -> (emoji, French label)
RUG_PRESENTATION: dict[RugRisk, tuple[str, str]] = {
    RugRisk.LOW: ("🟢", "RUG RISK FAIBLE"),
    RugRisk.MEDIUM: ("🟡", "RUG RISK MOYEN"),
    RugRisk.HIGH: ("🟠", "RUG RISK ÉLEVÉ"),
    RugRisk.CRITICAL: ("🔴", "RUG RISK CRITIQUE"),
    RugRisk.UNKNOWN: ("⚫", "SÉCURITÉ NON ANALYSÉE"),
}


@dataclass(slots=True)
class SafetyFinding:
    """One named thing that is wrong, or one thing we could not check."""

    code: str
    severity: Severity
    label_fr: str
    detail: str = ""
    # True when this alone forbids a purchase (spec §19).
    blocking: bool = False

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "label_fr": self.label_fr,
            "detail": self.detail,
            "blocking": self.blocking,
        }


@dataclass(slots=True)
class SafetyReport:
    """The verdict, with every finding named. Deliberately has no total."""

    address: str
    score: float | None
    rug_risk: RugRisk
    findings: list[SafetyFinding] = field(default_factory=list)
    coverage: float = 0.0
    analysed: bool = False
    engine_version: str = SAFETY_ENGINE_VERSION
    weights_fingerprint: str = ""

    @property
    def blocking_findings(self) -> list[SafetyFinding]:
        return [f for f in self.findings if f.blocking]

    @property
    def hard_veto(self) -> bool:
        """Does anything here forbid a purchase outright?

        Spec §19 lists RUG RISK HIGH alongside CRITICAL and honeypot, so the
        level alone blocks — found by running the scorer, where a token with
        three independent owner powers came out HIGH and still unvetoed.

        True also when the analysis is missing: authorising a BUY on a token
        nobody has checked is not a risk this product takes (spec §53).
        """
        return (
            bool(self.blocking_findings)
            or self.rug_risk in (RugRisk.HIGH, RugRisk.CRITICAL, RugRisk.UNKNOWN)
        )

    def counts(self) -> dict[str, int]:
        """Findings by severity. There is deliberately no total: summing would
        let five cosmetic warnings outweigh one honeypot."""
        out = {s.value: 0 for s in Severity}
        for f in self.findings:
            out[f.severity.value] += 1
        return out

    @property
    def emoji(self) -> str:
        return RUG_PRESENTATION[self.rug_risk][0]

    @property
    def label_fr(self) -> str:
        return RUG_PRESENTATION[self.rug_risk][1]

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "score": self.score,
            "score_label": f"{self.score:.0f}/100" if self.score is not None else "NON DISPONIBLE",
            "rug_risk": self.rug_risk.value,
            "rug_emoji": self.emoji,
            "rug_label_fr": self.label_fr,
            "hard_veto": self.hard_veto,
            "blocking": [f.to_dict() for f in self.blocking_findings],
            "findings": [f.to_dict() for f in self.findings],
            "counts": self.counts(),
            "coverage": round(self.coverage, 3),
            "analysed": self.analysed,
            "engine_version": self.engine_version,
            "weights_fingerprint": self.weights_fingerprint,
        }


def safety_fingerprint(settings: RobinhoodSettings) -> str:
    """Weights + thresholds, hashed. A changed threshold is a changed claim."""
    payload = json.dumps(
        {
            "weights": SAFETY_WEIGHTS,
            "version": SAFETY_ENGINE_VERSION,
            "min_coverage": settings.safety_min_coverage,
            "max_buy_tax": settings.safety_max_buy_tax,
            "max_sell_tax": settings.safety_max_sell_tax,
            "max_top10": settings.safety_max_top10_percent,
            "max_creator": settings.safety_max_creator_percent,
            "min_lp_locked": settings.safety_min_lp_locked_percent,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def unanalysed(address: str, settings: RobinhoodSettings, *, reason: str) -> SafetyReport:
    """The report for a token the security source said nothing about.

    Score is None, not zero: zero would sort it alongside a proven honeypot,
    and it is not one — it is unexamined.
    """
    return SafetyReport(
        address=address,
        score=None,
        rug_risk=RugRisk.UNKNOWN,
        findings=[SafetyFinding(
            code="NOT_ANALYSED",
            severity=Severity.UNKNOWN,
            label_fr="Sécurité non analysée",
            detail=reason,
            blocking=True,
        )],
        coverage=0.0,
        analysed=False,
        weights_fingerprint=safety_fingerprint(settings),
    )


def assess_safety(sec: TokenSecurity, settings: RobinhoodSettings) -> SafetyReport:
    """Score the contract facts and name every deduction."""
    findings: list[SafetyFinding] = []

    def add(code: str, severity: Severity, label: str, detail: str = "", *, blocking: bool = False) -> None:
        findings.append(SafetyFinding(code, severity, label, detail, blocking=blocking))

    # ---------------------------------------------------- 1. sellability --- #
    sellability = SAFETY_WEIGHTS["sellability"]
    if sec.is_honeypot is True:
        add("HONEYPOT", Severity.CRITICAL, "HONEYPOT — revente impossible",
            "le contrat empêche de vendre", blocking=True)
        sellability = 0.0
    elif sec.is_honeypot is None:
        add("HONEYPOT_UNKNOWN", Severity.UNKNOWN, "Test honeypot non effectué",
            "impossible de confirmer qu'on peut revendre")
        sellability *= 0.4
    if sec.cannot_sell_all is True:
        add("CANNOT_SELL_ALL", Severity.CRITICAL, "Vente totale impossible",
            "une partie de la position resterait bloquée", blocking=True)
        sellability = 0.0
    if sec.cannot_buy is True:
        add("CANNOT_BUY", Severity.CRITICAL, "Achat impossible", blocking=True)
        sellability = 0.0
    if sec.transfer_pausable is True:
        add("PAUSABLE", Severity.CRITICAL, "Transferts suspendables",
            "le propriétaire peut geler les échanges à tout moment", blocking=True)
        sellability = min(sellability, SAFETY_WEIGHTS["sellability"] * 0.2)
    if sec.trading_cooldown is True:
        add("COOLDOWN", Severity.MINOR, "Délai imposé entre transactions")
        sellability *= 0.9

    # ------------------------------------------------ 2. contract control --- #
    control = SAFETY_WEIGHTS["contract_control"]
    controls = (
        ("is_mintable", Severity.MAJOR, "Émission de nouveaux jetons possible", 0.5, False),
        ("owner_change_balance", Severity.CRITICAL, "Le propriétaire peut modifier les soldes", 0.0, True),
        ("can_take_back_ownership", Severity.MAJOR, "La propriété peut être reprise", 0.5, False),
        ("hidden_owner", Severity.CRITICAL, "Propriétaire caché", 0.0, True),
        ("selfdestruct", Severity.CRITICAL, "Le contrat peut s'autodétruire", 0.0, True),
        ("is_blacklisted", Severity.MAJOR, "Liste noire d'adresses", 0.6, False),
        ("is_proxy", Severity.MAJOR, "Contrat proxy modifiable", 0.6, False),
        ("external_call", Severity.MINOR, "Appels externes dans le contrat", 0.85, False),
    )
    for attr, severity, label, factor, blocking in controls:
        value = getattr(sec, attr)
        if value is True:
            add(attr.upper(), severity, label, blocking=blocking)
            control *= factor
    if sec.is_open_source is False:
        add("CLOSED_SOURCE", Severity.MAJOR, "Code source non vérifié",
            "impossible de savoir ce que fait le contrat")
        control *= 0.4
    elif sec.is_open_source is None:
        add("SOURCE_UNKNOWN", Severity.UNKNOWN, "Vérification du code non effectuée")
        control *= 0.7

    # ------------------------------------------------------------ 3. taxes --- #
    taxes = SAFETY_WEIGHTS["taxes"]
    for attr, cap, name in (
        ("buy_tax", settings.safety_max_buy_tax, "achat"),
        ("sell_tax", settings.safety_max_sell_tax, "vente"),
    ):
        value = getattr(sec, attr)
        if value is None:
            add(f"{attr.upper()}_UNKNOWN", Severity.UNKNOWN, f"Taxe à l'{name} inconnue")
            taxes *= 0.6
            continue
        # GoPlus returns a fraction ("0.05") on some chains and a percentage on
        # others. Values <= 1 are read as fractions, which is the documented
        # form; a 100% tax and a 1x multiplier are not distinguishable here, and
        # both are catastrophic anyway.
        pct = value * 100.0 if value <= 1.0 else value
        if pct >= 100.0:
            add(f"{attr.upper()}_TOTAL", Severity.CRITICAL, f"Taxe à l'{name} de 100 %",
                blocking=True)
            taxes = 0.0
        elif pct > cap:
            add(f"{attr.upper()}_HIGH", Severity.MAJOR, f"Taxe à l'{name} élevée", f"{pct:.1f} %")
            taxes *= 0.3
        elif pct > 0:
            add(f"{attr.upper()}_PRESENT", Severity.MINOR, f"Taxe à l'{name}", f"{pct:.1f} %")
            taxes *= 0.85
    if sec.slippage_modifiable is True:
        add("SLIPPAGE_MODIFIABLE", Severity.MAJOR, "Taxes modifiables après coup",
            "une taxe basse aujourd'hui peut devenir 100 % demain")
        taxes *= 0.3
    if sec.personal_slippage_modifiable is True:
        add("PERSONAL_SLIPPAGE", Severity.CRITICAL, "Taxe modifiable adresse par adresse",
            "votre adresse peut être taxée seule", blocking=True)
        taxes = 0.0

    # ------------------------------------------------------ 4. LP security --- #
    lp = SAFETY_WEIGHTS["lp_security"]
    locked = sec.lp_locked_percent
    if locked is None:
        add("LP_LOCK_UNKNOWN", Severity.UNKNOWN, "Verrouillage de la liquidité inconnu")
        lp *= 0.5
    elif locked < settings.safety_min_lp_locked_percent:
        severity = Severity.CRITICAL if locked < 10 else Severity.MAJOR
        add("LP_UNLOCKED", severity, "Liquidité non verrouillée",
            f"{locked:.0f} % verrouillé ou brûlé")
        lp *= 0.25 if severity is Severity.CRITICAL else 0.6

    # ----------------------------------------------------- 5. distribution --- #
    dist = SAFETY_WEIGHTS["distribution"]
    top10 = sec.top10_percent
    if top10 is None:
        add("TOP10_UNKNOWN", Severity.UNKNOWN, "Répartition des détenteurs inconnue")
        dist *= 0.5
    elif top10 > settings.safety_max_top10_percent:
        add("TOP10_CONCENTRATED", Severity.MAJOR, "Concentration des 10 premiers détenteurs",
            f"{top10:.0f} %")
        dist *= 0.4
    creator = sec.creator_percent
    if creator is not None and creator > settings.safety_max_creator_percent:
        add("CREATOR_HEAVY", Severity.MAJOR, "Le créateur détient une part importante", f"{creator:.0f} %")
        dist *= 0.4
    if sec.honeypot_with_same_creator is True:
        add("CREATOR_HONEYPOT_HISTORY", Severity.CRITICAL,
            "Le créateur a déjà déployé un honeypot", blocking=True)
        dist = 0.0
    if sec.is_airdrop_scam is True:
        add("AIRDROP_SCAM", Severity.CRITICAL, "Signalé comme arnaque à l'airdrop", blocking=True)
    if sec.fake_token is True:
        add("FAKE_TOKEN", Severity.CRITICAL, "Contrefaçon d'un token existant", blocking=True)

    score = max(0.0, min(100.0, sellability + control + taxes + lp + dist))

    # Coverage decides whether any of this is worth showing. Below the floor the
    # analysis is too thin to authorise anything — but the token is not thereby
    # bad, so the report says UNKNOWN rather than CRITICAL.
    report = SafetyReport(
        address=sec.address,
        score=score,
        rug_risk=RugRisk.LOW,
        findings=findings,
        coverage=sec.coverage,
        analysed=True,
        weights_fingerprint=safety_fingerprint(settings),
    )
    if sec.coverage < settings.safety_min_coverage:
        add("THIN_COVERAGE", Severity.UNKNOWN, "Analyse de sécurité incomplète",
            f"{sec.checks_present}/{sec.checks_total} contrôles disponibles", blocking=True)
        report.score = None
        report.rug_risk = RugRisk.UNKNOWN
        return report

    report.rug_risk = _rug_level(findings)

    # A veto that comes from the *level* rather than from one finding still has
    # to say so. Found by running the UI: a token vetoed for HIGH rug risk had
    # an empty `blocking` list, so the screen fell back to "Sécurité non
    # analysée" on a token that had been fully analysed. Every veto now carries
    # its own reason, and the UI never has to guess.
    if report.rug_risk is RugRisk.HIGH and not report.blocking_findings:
        majors = [f.label_fr for f in findings if f.severity is Severity.MAJOR]
        add("RUG_RISK_HIGH", Severity.MAJOR, "Risque de rug élevé",
            f"{len(majors)} signaux majeurs : {', '.join(majors[:3])}", blocking=True)

    # A veto zeroes the score rather than reducing it — the same rule the
    # explosion engine follows (invariant 40), and for the same reason. Found by
    # running the scorer: a proven honeypot came out at 70/100 because only the
    # sellability group was lost, and 70 reads as "fine" at a glance. A merely
    # reduced score would also let a trap outrank a sound token in a sorted
    # list. The zero always carries its reason in `blocking`.
    if report.blocking_findings:
        report.score = 0.0

    return report


def _rug_level(findings: list[SafetyFinding]) -> RugRisk:
    """Worst-finding-wins, then count. Never a sum.

    One CRITICAL is CRITICAL however clean everything else is. Beyond that,
    quantity of MAJORs escalates, because three independent ways for the owner
    to hurt you is a different situation from one.
    """
    criticals = sum(1 for f in findings if f.severity is Severity.CRITICAL)
    majors = sum(1 for f in findings if f.severity is Severity.MAJOR)
    if criticals:
        return RugRisk.CRITICAL
    if majors >= 3:
        return RugRisk.HIGH
    if majors >= 1:
        return RugRisk.MEDIUM
    return RugRisk.LOW
