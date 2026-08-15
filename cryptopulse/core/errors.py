"""Typed failure modes.

Rule #1 of this project: never silently substitute an estimate for a missing
value. Every failure to obtain data raises one of these, and the reason code
travels all the way to the API response and the dashboard.
"""

from __future__ import annotations

from enum import Enum


class Reason(str, Enum):
    """Machine-readable reason codes surfaced to the UI."""

    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    STALE_DATA = "STALE_DATA"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    RATE_LIMITED = "RATE_LIMITED"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"


class CryptoPulseError(Exception):
    """Base class. Carries a Reason so callers can branch without string matching."""

    reason: Reason = Reason.DATA_UNAVAILABLE

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict:
        return {"reason": self.reason.value, "message": self.message, "detail": self.detail}


class DataUnavailable(CryptoPulseError):
    """The provider answered, but the specific field/series does not exist."""

    reason = Reason.DATA_UNAVAILABLE


class SourceUnavailable(CryptoPulseError):
    """The provider itself could not be reached or returned a server error."""

    reason = Reason.SOURCE_UNAVAILABLE


class StaleData(CryptoPulseError):
    """Data was obtained but is older than the configured tolerance."""

    reason = Reason.STALE_DATA


class InsufficientHistory(CryptoPulseError):
    """Not enough candles to compute the requested feature honestly."""

    reason = Reason.INSUFFICIENT_HISTORY


class RateLimited(CryptoPulseError):
    """Provider signalled a rate limit (HTTP 429 / 418)."""

    reason = Reason.RATE_LIMITED

    def __init__(self, message: str, *, retry_after: float | None = None, detail: dict | None = None) -> None:
        super().__init__(message, detail=detail)
        self.retry_after = retry_after


class CircuitOpen(SourceUnavailable):
    """Local circuit breaker is open; we are deliberately not calling the provider."""

    reason = Reason.CIRCUIT_OPEN
