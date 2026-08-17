"""Shared async HTTP machinery: weighted rate limiting, retries, circuit breaker.

Exchanges ban IPs that ignore their published limits, and a banned IP takes the
whole scanner down. The budget here is enforced locally *before* a request goes
out, rather than reacting to 429s after the fact.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass

import httpx

from cryptopulse.core.errors import CircuitOpen, RateLimited, SourceUnavailable
from cryptopulse.core.logging import get_logger

log = get_logger("providers.http")

__all__ = ["WeightedRateLimiter", "CircuitBreaker", "HttpClient"]


class WeightedRateLimiter:
    """Sliding-window budget over request *weight*, not request count.

    Exchange REST limits are weight-based: one call for 500 klines can cost as
    much as fifty trivial calls. Counting requests would under- or over-shoot.
    """

    def __init__(self, weight_per_minute: int, window_seconds: float = 60.0) -> None:
        self.budget = weight_per_minute
        self.window = window_seconds
        self._events: list[tuple[float, int]] = []
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        self._events = [(t, w) for t, w in self._events if t > cutoff]

    @property
    def used(self) -> int:
        self._prune(time.monotonic())
        return sum(w for _, w in self._events)

    def remaining_pct(self) -> float:
        return max(0.0, 1.0 - self.used / self.budget) * 100.0

    async def acquire(self, weight: int = 1) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._prune(now)
                used = sum(w for _, w in self._events)
                if used + weight <= self.budget:
                    self._events.append((now, weight))
                    return
                oldest = self._events[0][0] if self._events else now
                wait = max(0.05, self.window - (now - oldest))
            log.debug("rate_limit_wait", seconds=round(wait, 2), used=used, budget=self.budget)
            await asyncio.sleep(wait)


@dataclass
class CircuitBreaker:
    """Stops hammering a provider that is clearly down.

    After `failure_threshold` consecutive failures the circuit opens for
    `reset_seconds`; the next call after that is a probe, and a success closes it.
    """

    failure_threshold: int = 5
    reset_seconds: float = 60.0
    _failures: int = 0
    _opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.reset_seconds:
            return False  # half-open: allow a probe
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic()

    def seconds_until_retry(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self.reset_seconds - (time.monotonic() - self._opened_at))


class HttpClient:
    """Thin httpx wrapper carrying the limiter, breaker and retry policy."""

    def __init__(
        self,
        base_url: str,
        *,
        name: str,
        weight_per_minute: int = 2400,
        max_concurrent: int = 8,
        timeout: float = 15.0,
        max_retries: int = 3,
        retry_base_delay: float = 0.5,
        circuit_failure_threshold: int = 5,
        circuit_reset_seconds: float = 60.0,
        headers: dict[str, str] | None = None,
        fallback_base_url: str | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.fallback_base_url = fallback_base_url.rstrip("/") if fallback_base_url else None
        self._active_base = self.base_url
        self.limiter = WeightedRateLimiter(weight_per_minute)
        self.breaker = CircuitBreaker(circuit_failure_threshold, circuit_reset_seconds)
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self._sem = asyncio.Semaphore(max_concurrent)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": "CryptoPulseAI/1.0", **(headers or {})},
            follow_redirects=True,
        )
        self.last_latency_ms: float | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def get_json(self, path: str, params: dict | None = None, *, weight: int = 1) -> object:
        return await self._request_json("GET", path, params=params, weight=weight)

    async def post_json(self, path: str, json_body: object, *, weight: int = 1) -> object:
        """POST with the same limiter, breaker and retry policy as GET.

        Exists for JSON-RPC (Robinhood Chain): every call is a POST to one path,
        and giving it a separate code path would mean a separate, unexercised
        retry policy.
        """
        return await self._request_json("POST", path, json_body=json_body, weight=weight)

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: object | None = None,
        weight: int = 1,
    ) -> object:
        if self.breaker.is_open:
            raise CircuitOpen(
                f"{self.name}: circuit open, retrying in {self.breaker.seconds_until_retry():.0f}s",
                detail={"provider": self.name},
            )

        await self.limiter.acquire(weight)
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            url = f"{self._active_base}{path}"
            started = time.perf_counter()
            try:
                async with self._sem:
                    resp = await self._client.request(method, url, params=params, json=json_body)
                self.last_latency_ms = (time.perf_counter() - started) * 1000

                if resp.status_code in (429, 418):
                    retry_after = float(resp.headers.get("Retry-After", self.retry_base_delay * (2**attempt)))
                    log.warning("rate_limited", provider=self.name, status=resp.status_code, retry_after=retry_after)
                    self.breaker.record_failure()
                    if attempt >= self.max_retries:
                        raise RateLimited(f"{self.name}: rate limited ({resp.status_code})", retry_after=retry_after)
                    await asyncio.sleep(retry_after)
                    continue

                if 500 <= resp.status_code < 600:
                    last_error = SourceUnavailable(f"{self.name}: HTTP {resp.status_code}")
                    self._maybe_switch_base(attempt)
                    if attempt >= self.max_retries:
                        self.breaker.record_failure()
                        raise last_error
                    await self._backoff(attempt)
                    continue

                if resp.status_code >= 400:
                    # A 4xx is our fault (bad symbol, bad params) — retrying will
                    # not help, and it must not trip the breaker.
                    self.breaker.record_success()
                    raise SourceUnavailable(
                        f"{self.name}: HTTP {resp.status_code} for {path}",
                        detail={"body": resp.text[:400], "params": params},
                    )

                self.breaker.record_success()
                return resp.json()

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = SourceUnavailable(f"{self.name}: {type(exc).__name__}: {exc}")
                log.warning("request_failed", provider=self.name, attempt=attempt, error=str(exc)[:200])
                self._maybe_switch_base(attempt)
                if attempt >= self.max_retries:
                    self.breaker.record_failure()
                    raise last_error from exc
                await self._backoff(attempt)

        raise last_error or SourceUnavailable(f"{self.name}: exhausted retries")

    def _maybe_switch_base(self, attempt: int) -> None:
        """Fail over to the mirror host once the primary has failed twice."""
        if self.fallback_base_url and attempt >= 1 and self._active_base != self.fallback_base_url:
            log.warning("switching_base_url", provider=self.name, to=self.fallback_base_url)
            self._active_base = self.fallback_base_url

    async def _backoff(self, attempt: int) -> None:
        # Full jitter: prevents a fleet of workers retrying in lockstep.
        delay = self.retry_base_delay * (2**attempt)
        await asyncio.sleep(random.uniform(0, delay))
