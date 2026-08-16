"""The one-line verdict: what a non-specialist should take away from a row.

Everything else in `scoring/` produces numbers and sentences for someone who
wants the detail. This produces four words for someone who does not, and it is
deliberately the *last* thing computed: it reads the score, the gates, the pump
maturity and the data confidence that already exist and compresses them. It
never introduces a new opinion, and it never sees data the score did not.

Four levels, and the rules that produce them:

  🔴 AVOID   — a hard gate failed (liquidity DANGEROUS, safety below floor), or
               the data behind the row is not trustworthy enough to judge.
  🟠 RISKY   — the score is real but the move is already mature, or a large risk
               penalty was applied. The opportunity may exist; the entry is bad.
  🟡 WATCH   — a genuine setup that has not triggered. Nothing to do yet.
  🟢 STRONG  — a good score, clean gates, a young move and a triggered or
               armed setup. All four, or it is not this level.

**A verdict is not advice and not a probability.** `🟢` means "this row cleared
every filter the scanner has", which is a statement about the scanner, not about
the future. The weights behind the score have never been fitted against real
outcomes (see CLAUDE.md §2), so a 🟢 today carries no measured success rate. The
`caveat` field says so on every single verdict, including the good ones, because
a badge with no caveat is exactly how a ranking gets read as a prediction.

Labels are carried in English and in French side by side: the product's owner
reads French, the code and the API read English, and neither should have to
translate the other at the point of use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from cryptopulse.scoring.states import SetupState

__all__ = ["VerdictLevel", "Verdict", "build_verdict"]

# Below this, nothing computed from the row is worth acting on.
MIN_CONFIDENCE_TO_JUDGE = 45.0
# A penalty this large means the risk report found something substantial.
HEAVY_PENALTY = 20.0


class VerdictLevel(str, Enum):
    STRONG = "STRONG"
    WATCH = "WATCH"
    RISKY = "RISKY"
    AVOID = "AVOID"


_PRESENTATION: dict[VerdictLevel, tuple[str, str, str]] = {
    # level: (emoji, English label, French label)
    VerdictLevel.STRONG: ("🟢", "Strong opportunity", "FORTE OPPORTUNITÉ"),
    VerdictLevel.WATCH: ("🟡", "Worth watching", "À SURVEILLER"),
    VerdictLevel.RISKY: ("🟠", "Risky", "RISQUÉ"),
    VerdictLevel.AVOID: ("🔴", "Avoid", "ÉVITER"),
}

_CAVEAT_EN = (
    "A verdict summarises the scanner's own filters. It is not advice and not a "
    "probability: these weights have never been validated against real outcomes."
)
_CAVEAT_FR = (
    "Ce verdict résume les filtres du scanner. Ce n'est ni un conseil ni une "
    "probabilité : ces pondérations n'ont jamais été validées sur des résultats réels."
)


@dataclass(slots=True)
class Verdict:
    level: VerdictLevel
    emoji: str
    label: str
    label_fr: str
    headline: str
    headline_fr: str
    reasons: list[str] = field(default_factory=list)
    reasons_fr: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "emoji": self.emoji,
            "label": self.label,
            "label_fr": self.label_fr,
            "headline": self.headline,
            "headline_fr": self.headline_fr,
            "reasons": self.reasons,
            "reasons_fr": self.reasons_fr,
            "caveat": _CAVEAT_EN,
            "caveat_fr": _CAVEAT_FR,
        }


def _make(level: VerdictLevel, headline: str, headline_fr: str, pairs: list[tuple[str, str]]) -> Verdict:
    emoji, label, label_fr = _PRESENTATION[level]
    return Verdict(
        level=level,
        emoji=emoji,
        label=label,
        label_fr=label_fr,
        headline=headline,
        headline_fr=headline_fr,
        reasons=[en for en, _ in pairs],
        reasons_fr=[fr for _, fr in pairs],
    )


def build_verdict(result) -> Verdict:
    """Compress one `ScoreResult` into a four-level verdict with its reasons.

    Typed loosely on purpose: importing `ScoreResult` here would make
    `scoring/engine.py` and this module import each other.
    """
    score = result.final_score
    maturity = result.maturity.score
    confidence = result.confidence.score
    state = result.state.state
    penalty = result.risk_penalty

    # ---- 🔴 the disqualifying conditions, checked first ------------------- #
    if result.liquidity.veto or result.safety.hard_veto:
        why = result.liquidity.reasons if result.liquidity.veto else result.safety.reasons
        detail = why[0] if why else "a hard risk gate rejected this asset"
        return _make(
            VerdictLevel.AVOID,
            "A hard risk gate rejected this asset",
            "Un filtre de risque bloquant a rejeté cet actif",
            [
                (detail, detail),
                (
                    "The row stays visible for information, but it can never be promoted.",
                    "La ligne reste visible à titre informatif, mais elle ne peut jamais être promue.",
                ),
            ],
        )

    if confidence < MIN_CONFIDENCE_TO_JUDGE:
        issue = result.confidence.issues[0] if result.confidence.issues else "data confidence is too low"
        return _make(
            VerdictLevel.AVOID,
            f"The data behind this row is not reliable enough to judge ({confidence:.0f}/100 confidence)",
            f"Les données de cette ligne ne sont pas assez fiables pour juger (confiance {confidence:.0f}/100)",
            [
                (issue, issue),
                (
                    "The scanner will not rate an asset it cannot see properly.",
                    "Le scanner ne note pas un actif qu'il ne voit pas correctement.",
                ),
            ],
        )

    # ---- 🟠 real signal, bad entry --------------------------------------- #
    if result.maturity.is_late:
        return _make(
            VerdictLevel.RISKY,
            f"The move looks already advanced (maturity {maturity:.0f}/100)",
            f"Le mouvement semble déjà avancé (maturité {maturity:.0f}/100)",
            [
                (
                    "This scanner is built to find moves before they run, and this one appears to have run.",
                    "Ce scanner cherche les mouvements avant qu'ils partent, et celui-ci semble déjà parti.",
                ),
                (
                    f"Opportunity score {score:.0f}/100 — the setup may be real, the entry is late.",
                    f"Score d'opportunité {score:.0f}/100 — le setup peut être réel, l'entrée est tardive.",
                ),
            ],
        )

    if penalty >= HEAVY_PENALTY:
        named = [p.reason for p in result.penalties.items][:2]
        return _make(
            VerdictLevel.RISKY,
            f"Sizeable risk deductions applied (−{penalty:.0f} points)",
            f"Déductions de risque importantes appliquées (−{penalty:.0f} points)",
            [(r, r) for r in named]
            or [
                (
                    "The risk report reduced the score substantially.",
                    "Le rapport de risque a fortement réduit le score.",
                )
            ],
        )

    # ---- 🟢 all four conditions, or it is not this level ------------------ #
    triggered = state in (SetupState.ARMED, SetupState.BREAKOUT, SetupState.RETEST, SetupState.CONTINUATION)
    if result.is_premium and triggered:
        return _make(
            VerdictLevel.STRONG,
            f"Clean setup, young move, score {score:.0f}/100",
            f"Setup propre, mouvement jeune, score {score:.0f}/100",
            [
                (
                    f"Setup state {state.value}: {result.state.rationale}",
                    f"État du setup {state.value} : {result.state.rationale}",
                ),
                (
                    f"Maturity {maturity:.0f}/100 — the move is not yet extended.",
                    f"Maturité {maturity:.0f}/100 — le mouvement n'est pas encore étiré.",
                ),
                (
                    f"Liquidity {result.liquidity.status.value}, data confidence {confidence:.0f}/100.",
                    f"Liquidité {result.liquidity.status.value}, confiance des données {confidence:.0f}/100.",
                ),
            ],
        )

    # ---- 🟡 everything else that scored at all --------------------------- #
    if state is SetupState.IGNORE or score < 35.0:
        return _make(
            VerdictLevel.WATCH,
            f"Nothing is happening here yet (score {score:.0f}/100)",
            f"Il ne se passe rien ici pour l'instant (score {score:.0f}/100)",
            [
                (
                    "No condition worth acting on. This is the normal state for most assets.",
                    "Aucune condition digne d'action. C'est l'état normal de la plupart des actifs.",
                )
            ],
        )

    pairs = [
        (
            f"Setup state {state.value}: {result.state.rationale}",
            f"État du setup {state.value} : {result.state.rationale}",
        )
    ]

    if triggered:
        # The setup has fired but did not clear the premium bar. Saying "has not
        # triggered" here would contradict the state shown right beside it.
        missing = []
        if score < 72.0:
            missing.append((
                f"the score is {score:.0f}, below the 72 needed",
                f"le score est de {score:.0f}, sous les 72 requis",
            ))
        if confidence < 60.0:
            missing.append((
                f"data confidence is {confidence:.0f}, below 60",
                f"la confiance des données est de {confidence:.0f}, sous 60",
            ))
        if missing:
            pairs.append(
                (
                    "Not promoted because " + " and ".join(en for en, _ in missing) + ".",
                    "Non promu car " + " et ".join(fr for _, fr in missing) + ".",
                )
            )
        return _make(
            VerdictLevel.WATCH,
            f"The setup has triggered but does not clear the bar (score {score:.0f}/100)",
            f"Le setup s'est déclenché mais ne passe pas la barre (score {score:.0f}/100)",
            pairs,
        )

    if result.state.trigger:
        pairs.append((f"Waiting for: {result.state.trigger}", f"En attente de : {result.state.trigger}"))
    return _make(
        VerdictLevel.WATCH,
        f"A setup is forming but has not triggered (score {score:.0f}/100)",
        f"Un setup se forme mais ne s'est pas déclenché (score {score:.0f}/100)",
        pairs,
    )
