"""Is Robinhood Chain really there? — the only definition, like verify.py's.

`providers/verify.py` owns the single definition of LIVE VERIFIED for a market
*feed* (tickers, klines, order book). This module owns the single definition
for the *chain*: same discipline, different object, and deliberately a separate
verdict — Robinhood Chain is never declared live because Binance is (spec §6),
and nothing here reads any Binance state.

THREE STATES, NOT TWO

The feed check is all-or-nothing because its five checks all probe one payload
contract, and a partial pass there means a mapping error. The chain check is
different: "the RPC answers, it is chain 4663, blocks exist" is one claim, and
"blocks are advancing right now / a log query works / the clock is coherent"
is a weaker, flakier one. Failing the whole verdict because one log query
timed out would make 🔴 mean two very different things. So:

  🟢 VERIFIED  — core checks AND liveness checks all pass
  🟡 PARTIAL   — the chain is reachable and is the right chain, but a liveness
                 check failed; nothing is presented as live-verified
  🔴 FAILED    — unreachable, wrong chain id, or no block to read

`verified` is True only for 🟢. PARTIAL licenses nothing — it exists so the
screen can say *which half* is broken instead of a bare red light.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.logging import get_logger
from cryptopulse.providers.robinhood import ROBINHOOD_CHAIN_ID, RobinhoodRpc

log = get_logger("providers.robinhood_verify")

__all__ = ["ChainState", "ChainCheck", "ChainVerification", "verify_robinhood_chain"]

# How long to wait between the two block-number samples of the progression
# check. Robinhood Chain produces ~100 ms blocks, so even one second should
# show dozens of new blocks; 1.5 s keeps the doctor fast while leaving margin.
PROGRESSION_WAIT_SECONDS = 1.5


class ChainState(str, Enum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


# state: (emoji, French label). The words BINANCE and ROBINHOOD never share a
# label; a test pins it.
PRESENTATION: dict[ChainState, tuple[str, str]] = {
    ChainState.PENDING: ("🟡", "VÉRIFICATION ROBINHOOD CHAIN EN COURS"),
    ChainState.VERIFIED: ("🟢", "ROBINHOOD CHAIN LIVE VERIFIED"),
    ChainState.PARTIAL: ("🟡", "ROBINHOOD CHAIN — VÉRIFICATION PARTIELLE"),
    ChainState.FAILED: ("🔴", "ROBINHOOD CHAIN NON VÉRIFIÉ"),
}

_NOTE = {
    ChainState.PENDING: "La vérification de la chaîne est en cours.",
    ChainState.VERIFIED: (
        "Un vrai aller-retour RPC a confirmé le chain id 4663, un dernier bloc daté de "
        "façon cohérente, des blocs qui avancent et une lecture de logs qui répond."
    ),
    ChainState.PARTIAL: (
        "La chaîne répond et c'est bien la 4663, mais une vérification de vivacité a "
        "échoué. Rien n'est présenté comme vérifié en direct tant que tout ne passe pas."
    ),
    ChainState.FAILED: (
        "Le RPC est injoignable, répond pour une autre chaîne, ou ne fournit aucun bloc. "
        "Aucune donnée Robinhood n'est présentée comme réelle."
    ),
}


@dataclass(slots=True)
class ChainCheck:
    name: str
    ok: bool
    detail: str = ""
    # Core checks decide FAILED vs not; liveness checks decide VERIFIED vs
    # PARTIAL. Recorded on the check so the split is data, not code spread out.
    core: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "core": self.core}


@dataclass(slots=True)
class ChainVerification:
    state: ChainState
    provider: str
    endpoint_host: str = ""
    chain_id: int | None = None
    latest_block: int | None = None
    latency_ms: float | None = None
    checks: list[ChainCheck] = field(default_factory=list)
    duration_ms: int = 0
    checked_at_ms: int | None = None
    error: str | None = None

    @property
    def verified(self) -> bool:
        # PARTIAL licenses nothing. Only the green state is "verified".
        return self.state is ChainState.VERIFIED

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
            "endpoint_host": self.endpoint_host,
            "chain_id": self.chain_id,
            "latest_block": self.latest_block,
            "latency_ms": self.latency_ms,
            "verified": self.verified,
            "passed": self.passed,
            "total": len(self.checks),
            "checks": [c.to_dict() for c in self.checks],
            "duration_ms": self.duration_ms,
            "checked_at_ms": self.checked_at_ms,
            "error": self.error,
            "note": _NOTE[self.state],
        }


async def verify_robinhood_chain(
    rpc: RobinhoodRpc,
    *,
    clock: Clock = SYSTEM_CLOCK,
    timeout_seconds: float | None = None,
    progression_wait: Callable[[], Awaitable[None]] | None = None,
) -> ChainVerification:
    """The Robinhood Chain doctor. Never raises.

    `progression_wait` is injectable so tests are not slowed by a real sleep;
    the default waits PROGRESSION_WAIT_SECONDS between the two block samples.
    """
    started = time.perf_counter()
    settings = rpc.settings
    result = ChainVerification(
        state=ChainState.PENDING, provider=rpc.name, endpoint_host=rpc.endpoint_host
    )

    async def default_wait() -> None:
        await asyncio.sleep(PROGRESSION_WAIT_SECONDS)

    wait = progression_wait or default_wait

    async def run() -> None:
        # -- core 1: the RPC answers at all -------------------------------- #
        try:
            chain_id = await rpc.chain_id()
            result.latency_ms = rpc.http.last_latency_ms
            result.chain_id = chain_id
            result.checks.append(
                ChainCheck(
                    "RPC joignable",
                    True,
                    f"{rpc.endpoint_host} — {result.latency_ms:.0f} ms" if result.latency_ms else rpc.endpoint_host,
                    core=True,
                )
            )
        except Exception as exc:
            result.checks.append(ChainCheck("RPC joignable", False, type(exc).__name__, core=True))
            result.error = f"{type(exc).__name__} en contactant {rpc.endpoint_host}"
            return

        # -- core 2: it is the right chain --------------------------------- #
        ok = chain_id == ROBINHOOD_CHAIN_ID
        result.checks.append(
            ChainCheck(
                f"chain id == {ROBINHOOD_CHAIN_ID}",
                ok,
                f"chaîne {chain_id}" + ("" if ok else " — mauvais réseau configuré dans CP_ROBINHOOD_RPC_URL"),
                core=True,
            )
        )
        if not ok:
            result.error = f"le RPC répond pour la chaîne {chain_id}, pas {ROBINHOOD_CHAIN_ID}"
            return

        # -- core 3: a latest block exists and is readable ------------------ #
        try:
            block = await rpc.latest_block()
            result.latest_block = block.number
            result.checks.append(
                ChainCheck("dernier bloc lisible", block.number > 0, f"bloc {block.number:,}", core=True)
            )
            if block.number <= 0:
                result.error = "le RPC ne renvoie aucun bloc"
                return
        except Exception as exc:
            result.checks.append(ChainCheck("dernier bloc lisible", False, type(exc).__name__, core=True))
            result.error = f"{type(exc).__name__} en lisant le dernier bloc"
            return

        # -- liveness 1: temporal coherence --------------------------------- #
        now_ms = clock.now_ms()
        lag_s = (now_ms - block.timestamp_ms) / 1000.0
        coherent = (
            -settings.max_block_future_seconds <= lag_s <= settings.max_block_lag_seconds
        )
        result.checks.append(
            ChainCheck(
                "horodatage du bloc cohérent avec l'horloge",
                coherent,
                f"bloc daté d'il y a {lag_s:.0f}s" if lag_s >= 0 else f"bloc daté {-lag_s:.0f}s dans le futur",
            )
        )

        # -- liveness 2: blocks are advancing ------------------------------- #
        try:
            first = await rpc.block_number()
            await wait()
            second = await rpc.block_number()
            advancing = second > first
            result.checks.append(
                ChainCheck(
                    "les blocs avancent",
                    advancing,
                    f"{first:,} → {second:,} (+{second - first})",
                )
            )
            result.latest_block = max(result.latest_block or 0, second)
        except Exception as exc:
            result.checks.append(ChainCheck("les blocs avancent", False, type(exc).__name__))

        # -- liveness 3: log reads work ------------------------------------- #
        try:
            span_end = result.latest_block or block.number
            logs = await rpc.get_logs(max(0, span_end - 4), span_end)
            # An empty list still proves the method works; on a chain doing
            # ~10M tx/day it would be surprising, so the count is displayed
            # rather than judged.
            result.checks.append(ChainCheck("lecture de logs", True, f"{len(logs)} log(s) sur 5 blocs"))
        except Exception as exc:
            result.checks.append(ChainCheck("lecture de logs", False, type(exc).__name__))

        # -- liveness 4 (optional): a known contract has code --------------- #
        # Skipped when unconfigured: no address could be verified from the
        # build environment, and checking a guessed one would be worse than
        # not checking.
        if settings.known_contract_address:
            try:
                code = await rpc.get_code(settings.known_contract_address)
                has_code = code not in ("", "0x", "0x0")
                result.checks.append(
                    ChainCheck(
                        "contrat connu présent",
                        has_code,
                        f"{settings.known_contract_address[:10]}… : {len(code) // 2 - 1 if has_code else 0} octets",
                    )
                )
            except Exception as exc:
                result.checks.append(ChainCheck("contrat connu présent", False, type(exc).__name__))

    try:
        if timeout_seconds:
            await asyncio.wait_for(run(), timeout=timeout_seconds)
        else:
            await run()
    except TimeoutError:
        result.error = f"vérification interrompue après {timeout_seconds:.0f}s"
        result.checks.append(ChainCheck("délai", False, result.error, core=True))
    except Exception as exc:  # a doctor that crashes is a doctor that lies by omission
        result.error = f"{type(exc).__name__}"

    result.duration_ms = int((time.perf_counter() - started) * 1000)
    result.checked_at_ms = clock.now_ms()

    core_ok = bool(result.checks) and all(c.ok for c in result.checks if c.core)
    all_ok = core_ok and all(c.ok for c in result.checks)
    if all_ok:
        result.state = ChainState.VERIFIED
    elif core_ok:
        result.state = ChainState.PARTIAL
    else:
        result.state = ChainState.FAILED

    log.info(
        "robinhood_chain_verification",
        state=result.state.value,
        passed=result.passed,
        total=len(result.checks),
        ms=result.duration_ms,
        block=result.latest_block,
    )
    return result
