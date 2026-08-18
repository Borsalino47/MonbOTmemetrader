"""Is this feed really live? — one definition, used by two callers.

WHY THIS IS ITS OWN MODULE

`cryptopulse doctor` has always answered this from the command line, blocking
until it finished. That was correct for a diagnostic and wrong for a startup: on
a phone the network round trips can take tens of seconds, and until phase 31 the
launcher would not start the server until they were done. The interface waited
for a network check it did not need in order to display a stored scan.

So the check moves in-process and runs in the background, which immediately
raises the risk that the two drift apart — a launcher reporting LIVE VERIFIED on
weaker evidence than `doctor` demands would be worse than not reporting at all.
This module is therefore the *only* definition of the verdict. `doctor` prints
its results with extra egress diagnosis; the service reads the same object.

WHAT "VERIFIED" MEANS HERE

Not "the host answered". A host can answer with a well-formed payload whose
fields are in the wrong order, which is precisely the failure this project was
written to be unable to hide. Verified means real data arrived **and
cross-checked against itself**:

  * the ticker's own low <= last <= high,
  * OHLC invariants hold on every returned candle,
  * candle spacing matches the timeframe that was requested,
  * the newest closed candle agrees with the ticker price,
  * the order book has bid < ask.

Any of those failing is a mapping error, and a mapping error means every number
downstream is wrong in a way no amount of scoring would reveal.

WHAT IT COSTS

Four requests: ping, ticker, klines, depth. That is the whole budget, and it is
why this can run at every start without being a burden.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.logging import get_logger
from cryptopulse.core.types import Timeframe

log = get_logger("providers.verify")

__all__ = ["FeedState", "Check", "FeedVerification", "verify_feed"]

# How far a kline close may sit from the ticker price before the two are
# considered to disagree. Generous: they are different endpoints sampled at
# slightly different moments, and a real mapping error is off by orders of
# magnitude rather than by a percent.
MAX_PRICE_DRIFT_PCT = 2.0


class FeedState(str, Enum):
    """Four states, and the fourth one matters.

    PENDING is not a failure — it is the honest answer for the first seconds
    after launch, and rendering it as "not verified" would make every start look
    broken for as long as the check takes.
    """

    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    # The fixture provider generates its candles. Verifying them against
    # themselves would always pass and would mean nothing.
    SKIPPED_SYNTHETIC = "SKIPPED_SYNTHETIC"


# state: (emoji, French label)
PRESENTATION: dict[FeedState, tuple[str, str]] = {
    FeedState.PENDING: ("🟡", "VÉRIFICATION BINANCE EN COURS"),
    FeedState.VERIFIED: ("🟢", "BINANCE — FLUX VÉRIFIÉ"),
    FeedState.FAILED: ("🔴", "FLUX LIVE NON VÉRIFIÉ"),
    FeedState.SKIPPED_SYNTHETIC: ("🟠", "DÉMO — AUCUN FLUX À VÉRIFIER"),
}


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(slots=True)
class FeedVerification:
    state: FeedState
    provider: str
    checks: list[Check] = field(default_factory=list)
    duration_ms: int = 0
    checked_at_ms: int | None = None
    error: str | None = None

    @property
    def verified(self) -> bool:
        return self.state is FeedState.VERIFIED

    @property
    def emoji(self) -> str:
        return PRESENTATION[self.state][0]

    @property
    def label_fr(self) -> str:
        return PRESENTATION[self.state][1]

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.ok)

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "emoji": self.emoji,
            "label_fr": self.label_fr,
            "provider": self.provider,
            "verified": self.verified,
            "passed": self.passed,
            "total": len(self.checks),
            "checks": [c.to_dict() for c in self.checks],
            "duration_ms": self.duration_ms,
            "checked_at_ms": self.checked_at_ms,
            "error": self.error,
            "note": _NOTE[self.state],
        }


_NOTE = {
    FeedState.PENDING: (
        "La vérification du flux est en cours. Les données affichées viennent du "
        "dernier scan enregistré ; leur âge est indiqué."
    ),
    FeedState.VERIFIED: (
        "Un vrai aller-retour réseau a renvoyé des données cohérentes avec elles-mêmes : "
        "prix dans le range 24h, invariants OHLC respectés, bougies alignées avec le "
        "ticker, carnet avec bid < ask."
    ),
    FeedState.FAILED: (
        "Le flux n'a pas pu être vérifié. Les scans précédents restent visibles avec leur "
        "âge, mais aucune nouvelle décision n'est présentée comme vérifiée en direct."
    ),
    FeedState.SKIPPED_SYNTHETIC: (
        "Le fournisseur synthétique génère ses bougies. Les vérifier contre elles-mêmes "
        "passerait toujours et ne voudrait rien dire."
    ),
}


async def verify_feed(
    provider,
    settings,
    *,
    clock: Clock = SYSTEM_CLOCK,
    timeout_seconds: float | None = None,
) -> FeedVerification:
    """Four requests, cross-checked. Never raises.

    `timeout_seconds` bounds the whole check rather than each request: the caller
    cares about "how long before I know", and a check that can take a minute is
    not usable at startup however honest each individual step is.
    """
    from cryptopulse.providers.registry import is_synthetic

    started = time.perf_counter()
    name = provider.name

    if is_synthetic(provider):
        return FeedVerification(
            state=FeedState.SKIPPED_SYNTHETIC,
            provider=name,
            checked_at_ms=clock.now_ms(),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    result = FeedVerification(state=FeedState.PENDING, provider=name)
    ref = provider.reference_symbol

    async def run() -> None:
        # 1. Does the host answer at all?
        try:
            health = await provider.health()
            result.checks.append(
                Check("connectivité", health.available, health.detail or f"{health.latency_ms:.0f} ms")
            )
            if not health.available:
                result.error = health.detail or "provider unreachable"
                return
        except Exception as exc:
            result.checks.append(Check("connectivité", False, f"{type(exc).__name__}"))
            result.error = f"{type(exc).__name__} en contactant {name}"
            return

        # 2. Ticker, checked against its own 24h range.
        ticker = None
        try:
            tickers = await provider.get_tickers_24h([ref])
            ticker = tickers.get(ref)
            ok = (
                ticker is not None
                and ticker.last_price > 0
                and ticker.low_24h <= ticker.last_price <= ticker.high_24h
            )
            result.checks.append(
                Check(
                    "prix dans son propre range 24h",
                    ok,
                    f"{ref} {ticker.last_price:,.2f}" if ticker else f"aucune ligne {ref}",
                )
            )
        except Exception as exc:
            result.checks.append(Check("ticker 24h", False, f"{type(exc).__name__}"))

        # 3. Klines: the field-order check that matters most. A payload with the
        #    columns in the wrong order is well-formed and completely wrong, and
        #    nothing downstream would reveal it.
        try:
            series = await provider.get_ohlcv(ref, Timeframe.M5, 60)
            n = len(series)
            invariants = bool(
                (series.high >= series.low).all()
                and (series.high >= series.open).all()
                and (series.high >= series.close).all()
                and (series.low <= series.open).all()
                and (series.low <= series.close).all()
            )
            spacing = (
                bool(((series.open_time_ms[1:] - series.open_time_ms[:-1]) == Timeframe.M5.ms).all())
                if n > 1
                else True
            )
            result.checks.append(
                Check(
                    "invariants OHLC + espacement des bougies",
                    invariants and spacing and n > 10,
                    f"{n} bougies, invariants={invariants}, espacement={spacing}",
                )
            )

            # 4. Two independent endpoints must agree about the price.
            if ticker is not None and n > 0:
                closed = series.closed()
                drift = abs(closed.last_close - ticker.last_price) / ticker.last_price * 100
                result.checks.append(
                    Check(
                        "bougies et ticker d'accord sur le prix",
                        drift < MAX_PRICE_DRIFT_PCT,
                        f"{drift:.3f}% d'écart",
                    )
                )
        except Exception as exc:
            result.checks.append(Check("klines", False, f"{type(exc).__name__}"))

        # 5. Order book sanity.
        try:
            book = await provider.get_order_book(ref, 20)
            ok = (
                book.best_bid is not None
                and book.best_ask is not None
                and book.best_bid < book.best_ask
            )
            result.checks.append(
                Check(
                    "carnet : meilleure offre < meilleure demande",
                    ok,
                    f"spread {book.spread_bps:.2f} bps" if book.spread_bps is not None else "",
                )
            )
        except Exception as exc:
            result.checks.append(Check("carnet d'ordres", False, f"{type(exc).__name__}"))

    try:
        if timeout_seconds:
            import asyncio

            await asyncio.wait_for(run(), timeout=timeout_seconds)
        else:
            await run()
    except TimeoutError:
        result.error = f"vérification interrompue après {timeout_seconds:.0f}s"
        result.checks.append(Check("délai", False, result.error))
    except Exception as exc:
        result.error = f"{type(exc).__name__}"

    result.duration_ms = int((time.perf_counter() - started) * 1000)
    result.checked_at_ms = clock.now_ms()
    # Every check must pass. A partial pass is a mapping error somewhere, and
    # "mostly verified" is not a state this project is willing to display.
    result.state = (
        FeedState.VERIFIED
        if result.checks and all(c.ok for c in result.checks)
        else FeedState.FAILED
    )
    log.info(
        "feed_verification",
        provider=name, state=result.state.value,
        passed=result.passed, total=len(result.checks), ms=result.duration_ms,
    )
    return result
