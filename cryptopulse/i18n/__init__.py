"""Localisation. One catalogue, rendered at emission, never at display time.

WHY THE TEXT IS BUILT IN PYTHON AND NOT IN REACT

The reasons a score gives are produced where the numbers are: the component
that knows RVOL is 2.59 is the only place that can say so. Sending an English
sentence to the front end and translating it there would mean parsing prose to
recover the numbers — and every new reason would silently ship in English until
someone noticed.

So the sentence is assembled here, in French, from a code and its parameters.

WHAT `Text` IS FOR

`Text` is a `str` subclass carrying `.code` and `.params`. Everything
downstream — `list[str]`, JSON serialisation, `"x" in reasons`, the database —
keeps working unchanged, while the structured form travels with it for anything
that wants to branch on the code rather than on the words (spec §14).

NO NETWORK, NO MODEL, NO LOOKUP AT RENDER TIME

Templates are module constants and formatting is `str.format`. A reason costs
one string interpolation, which is why this can run inside the scan loop
without being measured (spec §15, §23).
"""

from __future__ import annotations

__all__ = ["Text", "msg", "num", "pct", "mult", "money", "signed", "price"]


class Text(str):
    """A rendered French sentence that remembers where it came from."""

    __slots__ = ("code", "params")

    def __new__(cls, rendered: str, code: str = "", params: dict | None = None) -> Text:
        obj = super().__new__(cls, rendered)
        obj.code = code
        obj.params = params or {}
        return obj

    def to_dict(self) -> dict:
        return {"code": self.code, "text": str(self), "params": self.params}


def msg(code: str, template: str):
    """Bind a code to a French template, returning a callable that renders it.

    The code is not decoration: it is what a future UI, an alert router or a
    statistics bucket can key on without matching on prose that may be
    reworded.
    """

    def render(**params) -> Text:
        return Text(template.format(**params), code=code, params=params)

    render.code = code           # type: ignore[attr-defined]
    render.template = template   # type: ignore[attr-defined]
    return render


# --------------------------------------------------------------------------- #
# Number formatting — French conventions, applied once, here
# --------------------------------------------------------------------------- #
#
# Decimal comma, narrow no-break space before the percent sign and as the
# thousands separator. Doing this at the formatting layer rather than at each
# call site is what stops "2.59" and "2,59" appearing on the same screen.

_NBSP = " "  # narrow no-break space, the French typographic separator


def num(value: float | None, nd: int = 2) -> str:
    """`2.59` -> `2,59`. None renders as an em dash, never as zero."""
    if value is None:
        return "—"
    text = f"{value:,.{nd}f}"
    # en_US grouping -> French grouping, in one pass so the two separators
    # cannot collide halfway through.
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", _NBSP)


def signed(value: float | None, nd: int = 2) -> str:
    """Same, with an explicit sign — used where direction is the point."""
    if value is None:
        return "—"
    return ("+" if value > 0 else "") + num(value, nd)


def pct(value: float | None, nd: int = 0) -> str:
    """`51.0` -> `+51 %`. The space before `%` is required in French."""
    if value is None:
        return "—"
    return f"{signed(value, nd)}{_NBSP}%"


def mult(value: float | None, nd: int = 2) -> str:
    """`2.59` -> `2,59×`. The multiplication sign, not the letter x."""
    if value is None:
        return "—"
    return f"{num(value, nd)}×"


def money(value: float | None, nd: int = 0) -> str:
    """`34000.5` -> `34 000 $`."""
    if value is None:
        return "—"
    return f"{num(value, nd)}{_NBSP}$"


def price(value: float | None) -> str:
    """A price, at the significance the venue quotes it, with a decimal comma.

    `:.8g` is kept because it is what the rest of the system uses to avoid
    inventing precision on a token quoted at 0.00000042; only the separator
    changes.
    """
    if value is None:
        return "—"
    return f"{value:.8g}".replace(".", ",")
