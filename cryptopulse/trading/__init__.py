"""Turning scores into a decision, and following what happens next.

Everything below this package answers "what is true about this asset?".
This package answers "so what do I do?", and then keeps asking it about the
positions the user says they opened.

**No module here can place an order.** There is no exchange key, no signing, no
private key and no order endpoint anywhere in this repository. The workflow is
analyse -> decide -> alert -> *the user acts manually* -> the user confirms ->
measure. A test asserts that this stays true.
"""

from __future__ import annotations

__all__: list[str] = []
