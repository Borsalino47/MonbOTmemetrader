"""One token, seen by every source at once — and where they disagree.

`doctor` cross-checks Binance's ticker against its own klines because a
well-formed payload with the fields in the wrong order is invisible from the
inside. This is the same idea for an on-chain token: GeckoTerminal and
DexScreener index the same pools from the same chain, so a price they disagree
about is a price nobody should act on.

THE RULE THIS MODULE EXISTS TO ENFORCE

**Two sources are reported side by side, never merged.** No average, no
"preferred source wins", no silent fallback. Averaging two prices 40 % apart
produces a number describing neither and destroys the only signal that mattered
— that something is wrong with this token's data. Each figure keeps its source
label, and a disagreement becomes a named caveat the decision layer can read.

WHAT AGREEMENT IS AND IS NOT

Agreement here is a statement about *our data*, not about the token. Two
indexers agreeing does not make a token safe, and a token with one source is
not thereby suspicious — it is thinly indexed, which is ordinary for something
minted twenty minutes ago. The states are AGREE / DISAGREE / SINGLE_SOURCE /
NO_DATA, and they are four different sentences.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from cryptopulse.config.settings import RobinhoodSettings
from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.logging import get_logger
from cryptopulse.hunter.robinhood_discovery import TokenCandidate
from cryptopulse.providers.dexscreener import DexScreenerClient, PairSnapshot
from cryptopulse.providers.robinhood import ROBINHOOD_CHAIN_ID, ROBINHOOD_PROVIDER_ID

log = get_logger("hunter.robinhood_detail")

__all__ = ["Agreement", "FieldComparison", "CrossCheck", "TokenDetail", "cross_check", "build_detail"]


class Agreement(str, Enum):
    AGREE = "AGREE"
    DISAGREE = "DISAGREE"
    # One source has it, the other does not. Ordinary for a fresh token, and
    # not a defect — but it does mean nothing has been verified.
    SINGLE_SOURCE = "SINGLE_SOURCE"
    NO_DATA = "NO_DATA"


AGREEMENT_LABEL: dict[Agreement, tuple[str, str]] = {
    Agreement.AGREE: ("✓", "Deux sources concordantes"),
    Agreement.DISAGREE: ("⚠️", "Les deux sources ne sont pas d'accord"),
    Agreement.SINGLE_SOURCE: ("·", "Une seule source — rien à recouper"),
    Agreement.NO_DATA: ("—", "Aucune source"),
}


@dataclass(slots=True)
class FieldComparison:
    """One figure as each source reports it. Deliberately keeps both."""

    field: str
    label_fr: str
    geckoterminal: float | None
    dexscreener: float | None
    agreement: Agreement
    drift_pct: float | None = None

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "label_fr": self.label_fr,
            # Both values, always. There is no merged value to fall back on,
            # which is what stops a caller quietly picking one.
            "geckoterminal": self.geckoterminal,
            "dexscreener": self.dexscreener,
            "agreement": self.agreement.value,
            "drift_pct": self.drift_pct,
        }


@dataclass(slots=True)
class CrossCheck:
    comparisons: list[FieldComparison] = field(default_factory=list)
    sources_seen: list[str] = field(default_factory=list)

    @property
    def agreement(self) -> Agreement:
        """The worst state across the compared figures."""
        states = [c.agreement for c in self.comparisons]
        if not states:
            return Agreement.NO_DATA
        for level in (Agreement.DISAGREE, Agreement.SINGLE_SOURCE, Agreement.NO_DATA):
            if level in states:
                return level
        return Agreement.AGREE

    @property
    def disagreements(self) -> list[FieldComparison]:
        return [c for c in self.comparisons if c.agreement is Agreement.DISAGREE]

    @property
    def caveats(self) -> list[str]:
        """What the decision layer must be told, in words a person can read."""
        out: list[str] = []
        for c in self.disagreements:
            out.append(
                f"{c.label_fr} : {c.drift_pct:.0f} % d'écart entre les deux sources"
                if c.drift_pct is not None
                else f"{c.label_fr} : sources en désaccord"
            )
        if self.agreement is Agreement.SINGLE_SOURCE:
            out.append("Une seule source indexe ce token — aucun recoupement possible")
        return out

    def to_dict(self) -> dict:
        emoji, label = AGREEMENT_LABEL[self.agreement]
        return {
            "agreement": self.agreement.value,
            "emoji": emoji,
            "label_fr": label,
            "comparisons": [c.to_dict() for c in self.comparisons],
            "disagreements": [c.to_dict() for c in self.disagreements],
            "caveats": self.caveats,
            "sources_seen": self.sources_seen,
        }


def _drift_pct(a: float, b: float) -> float | None:
    """Relative difference, as a percentage of the larger magnitude.

    Symmetric on purpose: which source is "reference" is not a question this
    module is entitled to answer, and dividing by one of them would make the
    drift depend on that arbitrary choice.
    """
    scale = max(abs(a), abs(b))
    if scale == 0:
        return 0.0 if a == b else None
    return abs(a - b) / scale * 100.0


def _compare(
    field_name: str, label: str, gt: float | None, ds: float | None, tolerance_pct: float
) -> FieldComparison:
    if gt is None and ds is None:
        return FieldComparison(field_name, label, None, None, Agreement.NO_DATA)
    if gt is None or ds is None:
        return FieldComparison(field_name, label, gt, ds, Agreement.SINGLE_SOURCE)
    drift = _drift_pct(gt, ds)
    if drift is None:
        return FieldComparison(field_name, label, gt, ds, Agreement.DISAGREE)
    state = Agreement.AGREE if drift <= tolerance_pct else Agreement.DISAGREE
    return FieldComparison(field_name, label, gt, ds, state, round(drift, 2))


def cross_check(
    candidate: TokenCandidate, pairs: list[PairSnapshot], settings: RobinhoodSettings
) -> CrossCheck:
    """Compare what each source says about the same token."""
    sources: list[str] = []
    if candidate.pools:
        sources.append("GECKOTERMINAL")
    if pairs:
        sources.append("DEXSCREENER")

    # DexScreener's per-token answer is a list of pairs; the comparable figures
    # are the deepest pair's price and the sum of liquidity, mirroring exactly
    # what the discovery stage does with GeckoTerminal's pools.
    known_liq = [p.liquidity_usd for p in pairs if p.liquidity_usd is not None]
    ds_liquidity = sum(known_liq) if known_liq else None
    deepest = max(
        (p for p in pairs if p.liquidity_usd is not None),
        key=lambda p: p.liquidity_usd or 0.0,
        default=(pairs[0] if pairs else None),
    )
    ds_price = deepest.price_usd if deepest else None
    ds_fdv = deepest.fdv_usd if deepest else None

    check = CrossCheck(sources_seen=sources)
    check.comparisons = [
        _compare("price_usd", "Prix", candidate.price_usd, ds_price,
                 settings.crosscheck_max_price_drift_pct),
        _compare("liquidity_usd", "Liquidité", candidate.liquidity_usd, ds_liquidity,
                 settings.crosscheck_max_liquidity_drift_pct),
        _compare("fdv_usd", "FDV", candidate.fdv_usd, ds_fdv,
                 settings.crosscheck_max_liquidity_drift_pct),
    ]
    return check


@dataclass(slots=True)
class TokenDetail:
    """Everything known about one token, source by source."""

    candidate: TokenCandidate
    ds_pairs: list[PairSnapshot] = field(default_factory=list)
    crosscheck: CrossCheck | None = None
    errors: list[str] = field(default_factory=list)
    requests: int = 0

    def to_dict(self) -> dict:
        row = self.candidate.to_dict()
        return {
            **row,
            "provider": ROBINHOOD_PROVIDER_ID,
            "chain_id": ROBINHOOD_CHAIN_ID,
            # Pools, kept per source rather than merged into one list: two
            # indexers listing the same pool differently is information.
            "pools": {
                "geckoterminal": [p.to_dict() for p in self.candidate.pools],
                "dexscreener": [p.to_dict() for p in self.ds_pairs],
            },
            "crosscheck": self.crosscheck.to_dict() if self.crosscheck else None,
            "errors": self.errors,
            "requests": self.requests,
        }


async def build_detail(
    candidate: TokenCandidate,
    dexscreener: DexScreenerClient,
    settings: RobinhoodSettings,
    *,
    clock: Clock = SYSTEM_CLOCK,
) -> TokenDetail:
    """Assemble the full view of one token. Never raises."""
    detail = TokenDetail(candidate=candidate)
    try:
        detail.requests += 1
        detail.ds_pairs = await dexscreener.token_pairs(candidate.address)
    except Exception as exc:
        detail.errors.append(f"dexscreener: {type(exc).__name__}")
        log.warning("dexscreener_lookup_failed", error=type(exc).__name__)

    detail.crosscheck = cross_check(candidate, detail.ds_pairs, settings)
    log.info(
        "robinhood_detail",
        token=candidate.address[:10],
        agreement=detail.crosscheck.agreement.value,
        ds_pairs=len(detail.ds_pairs),
    )
    return detail
