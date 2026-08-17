"""Deep scan: the expensive look, spent only on what the pre-scan selected.

The pre-scan ranks the whole venue for free but sees no price history at all —
it works from a single ticker snapshot. This stage fetches the candles, which is
where the cost lives: four timeframes per symbol, so 40 candidates is ~160 kline
requests. That is affordable now only because the candle cache removed 88.8% of
the scan's own requests; before it, this stage had no budget to run in.

Three rules keep it cheap and honest:

1. **Never re-fetch what the scan already has.** A candidate that is also in the
   classic scanner's universe is already fully analysed this cycle. The deep
   scan reuses that result rather than paying for it twice, and says how many it
   reused.

2. **The budget is stated and enforced.** `max_symbols` caps the work before it
   starts, and the report says what it cost. A search that quietly spent 400
   requests would be discovered as a rate-limit ban, not as a log line.

3. **One symbol failing never fails the search.** Same rule as the scanner:
   errors are collected per symbol and reported, and the rest still returns.

The output pairs each candidate with a `TOKEN_DISCOVERY_SCORE` — "has this
token's behaviour just changed?" — and, when available, the ordinary
`ScoreResult` so the two questions can be read side by side. They are
deliberately not blended into one number.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.core.clock import SYSTEM_CLOCK, Clock
from cryptopulse.core.logging import get_logger
from cryptopulse.features.pipeline import AssetFeatures
from cryptopulse.hunter.discovery import Candidate
from cryptopulse.risk.liquidity import assess_liquidity
from cryptopulse.scoring.discovery import DiscoveryEngine, DiscoveryResult
from cryptopulse.scoring.engine import ScoreResult

log = get_logger("hunter.deep")

__all__ = ["DeepScanResult", "DeepScanReport", "DeepScanner"]


@dataclass(slots=True)
class DeepScanResult:
    candidate: Candidate
    discovery: DiscoveryResult
    opportunity: ScoreResult | None = None
    reused_from_scan: bool = False

    def to_dict(self) -> dict:
        d = {
            "candidate": self.candidate.to_dict(),
            "discovery": self.discovery.to_dict(),
            "reused_from_scan": self.reused_from_scan,
        }
        if self.opportunity is not None:
            o = self.opportunity
            # The two scores answer different questions and are shown apart, never
            # averaged: blending them would hide which one carried the signal.
            d["opportunity"] = {
                "final_score": round(o.final_score, 2),
                "opportunity_label": f"{o.final_score:.0f}/100",
                "state": o.state.state.value,
                "pump_maturity": round(o.maturity.score, 1),
                "data_confidence": round(o.confidence.score, 1),
                "liquidity": o.liquidity.status.value,
                "verdict": o.verdict(),
            }
        else:
            d["opportunity"] = None
        return d


@dataclass(slots=True)
class DeepScanReport:
    examined: int = 0
    reused: int = 0
    fetched: int = 0
    kline_requests: int = 0
    results: list[DeepScanResult] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    duration_ms: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "examined": self.examined,
            "reused_from_scan": self.reused,
            "newly_fetched": self.fetched,
            "kline_requests": self.kline_requests,
            "errors": self.errors,
            "duration_ms": self.duration_ms,
            "notes": self.notes,
            "results": [r.to_dict() for r in self.results],
        }


class DeepScanner:
    """Turns pre-scan candidates into scored ones, reusing whatever it can."""

    def __init__(
        self,
        settings: CryptoPulseSettings,
        scanner,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.settings = settings
        # The classic scanner is borrowed rather than re-implemented: its
        # `_fetch_features` already carries the closed() boundary, the per-symbol
        # error isolation and the structure configuration.
        self.scanner = scanner
        self.clock = clock
        self.engine = DiscoveryEngine(settings)

    async def run(self, candidates: list[Candidate], *, max_symbols: int = 40) -> DeepScanReport:
        t0 = time.perf_counter()
        report = DeepScanReport()
        if not candidates:
            report.notes.append("no candidates to examine")
            return report

        wanted = candidates[:max_symbols]
        report.examined = len(wanted)
        if len(candidates) > max_symbols:
            report.notes.append(
                f"{len(candidates)} candidates offered, {max_symbols} examined — the rest "
                "n'ont pas été analysés plutôt qu'analysés mal."
            )

        # What the classic scan already produced this cycle, free to reuse.
        already = {
            r.symbol: r for r in (self.scanner.last_report.results if self.scanner.last_report else [])
        }
        tickers = self.scanner.last_tickers or {}
        timeframes = len(self.settings.scanner.timeframes)
        sem = asyncio.Semaphore(self.settings.providers.max_concurrent_requests)

        async def examine(c: Candidate) -> tuple[Candidate, AssetFeatures | None, ScoreResult | None, str | None, bool]:
            existing = already.get(c.symbol)
            if existing is not None and existing.features is not None:
                return c, existing.features, existing, None, True
            async with sem:
                try:
                    af = await self.scanner._fetch_features(c.symbol, tickers.get(c.symbol))
                    return c, af, None, None, False
                except Exception as exc:  # one candidate must not fail the search
                    return c, None, None, f"{type(exc).__name__}: {exc}", False

        gathered = await asyncio.gather(*(examine(c) for c in wanted))

        now_ms = self.clock.now_ms()
        for c, af, opportunity, err, reused in gathered:
            if err is not None:
                report.errors[c.symbol] = err
                continue
            if af is None:
                report.errors[c.symbol] = "no features produced"
                continue

            if reused:
                report.reused += 1
            else:
                report.fetched += 1
                report.kline_requests += timeframes

            # Maturity and liquidity come from the opportunity path when it ran,
            # and are computed here otherwise. Neither is invented: a candidate
            # whose inputs are missing gets `None` and the component says so.
            maturity = opportunity.maturity.score if opportunity is not None else None
            if opportunity is not None:
                liquidity = opportunity.liquidity.status
            else:
                liquidity = assess_liquidity(af, self.settings.risk).status

            discovery = self.engine.score(
                af,
                timestamp_ms=now_ms,
                volume_excess_vs_yesterday=c.volume_excess_vs_yesterday,
                maturity_score=maturity,
                liquidity_status=liquidity,
                range_position=c.range_position,
            )
            report.results.append(
                DeepScanResult(
                    candidate=c,
                    discovery=discovery,
                    opportunity=opportunity,
                    reused_from_scan=reused,
                )
            )

        report.results.sort(key=lambda r: r.discovery.score, reverse=True)
        report.duration_ms = int((time.perf_counter() - t0) * 1000)

        log.info(
            "deep_scan_complete",
            examined=report.examined,
            reused=report.reused,
            fetched=report.fetched,
            kline_requests=report.kline_requests,
            failed=len(report.errors),
            duration_ms=report.duration_ms,
        )
        return report
