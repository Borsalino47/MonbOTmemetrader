"""Token Hunter: finding assets the classic scanner structurally cannot see.

The classic scanner ranks the top 120 pairs by 24h volume, which excludes by
construction any token nobody is watching yet. This package answers the other
question — "is there something interesting that I am not already following?" —
by reading the whole venue from the single ticker call the scan already makes,
and handing a short list of candidates to the expensive deep scan.
"""

from cryptopulse.hunter.deep import DeepScanner, DeepScanReport, DeepScanResult
from cryptopulse.hunter.discovery import (
    Candidate,
    PrescanReport,
    SnapshotMemory,
    TokenSnapshot,
    prescan,
)

__all__ = [
    "Candidate", "PrescanReport", "SnapshotMemory", "TokenSnapshot", "prescan",
    "DeepScanner", "DeepScanReport", "DeepScanResult",
]
