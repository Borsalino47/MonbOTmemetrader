"""Pump history: what a token's past accelerations were, and whether now looks like them.

Detection is threshold-free — episodes are found and their size recorded, so one
pass answers every question about "+3%" or "+20%". Statistics carry their sample
size, and the similarity comparison refuses to print a rate below the floor,
which on a few weeks of history is the common case rather than the exception.
"""

from cryptopulse.pumps.detect import (
    DEFAULT_PUMP_CONFIG,
    PumpConfig,
    PumpEpisode,
    PumpHistory,
    build_history,
    find_pumps,
)
from cryptopulse.pumps.stats import (
    PumpStats,
    SetupFingerprint,
    SimilarityReport,
    compare_to_past,
    summarise,
)

__all__ = [
    "PumpConfig", "PumpEpisode", "PumpHistory", "find_pumps", "build_history",
    "DEFAULT_PUMP_CONFIG", "PumpStats", "summarise",
    "SetupFingerprint", "SimilarityReport", "compare_to_past",
]
