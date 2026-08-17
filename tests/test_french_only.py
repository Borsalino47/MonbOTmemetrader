"""No English reaches the screen — enforced, not audited once.

WHY A TEST AND NOT A CHECKLIST

A one-off sweep translates what exists today. The next engine, the next
component, the next error message ships in English and nobody notices until it
is on a phone. These tests read the source and the payloads and fail on the
words themselves, so a regression is a red build rather than a discovery.

WHAT IS DELIBERATELY ALLOWED

  * enum *values* (BREAKOUT, SELL, DANGEROUS) — identifiers stored in SQLite
    and compared in code, which the user explicitly allowed to stay English
    (spec §1). What is checked is that a French label travels beside them.
  * trading acronyms — RVOL, RSI, ATR, MACD, MFE, MAE, FDV, PnL (spec §8).
  * code identifiers, docstrings, comments and log messages: developer surface,
    not user surface.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Words that, appearing in a user-facing string, mean something was not
# translated. Drawn from spec §20, minus the ones that are the *same word* in
# French — "volume", "prix"/"price" as a heading, "support", "momentum",
# "acceptable". Flagging those would make the test cry wolf on correct French
# and it would be switched off within a week, which is worse than not having it.
ENGLISH_MARKERS = (
    "why", "risks", "trigger", "breakout", "retest", "bullish", "bearish",
    "strong", "weak", "dangerous", "buy", "sell", "hold", "avoid", "watch",
    "unavailable", "unknown", "insufficient", "already", "still", "below",
    "above", "rising", "falling", "the ", " is ", " are ", " and ", " not ",
    " with ", " from ", " of the ", "candle", "range ", "widest",
)

# "retest" and "setup" are kept deliberately: spec §3 lists RETEST as the
# French label itself, and both are the words French traders actually use.
LOANWORDS = ("retest", "setup", "momentum", "range position")

# Acronyms and proper nouns that are correct French usage as they stand.
ALLOWED = re.compile(
    r"\b(RVOL|RSI|ATR|MACD|MFE|MAE|FDV|PnL|API|URL|HTTP|JSON|SQLite|Termux|"
    r"Binance|Kraken|Robinhood|GeckoTerminal|DexScreener|GoPlus|Uniswap|"
    r"Bollinger|EMA\d*|WETH|USDC|USDT|ETH|BTC|DEX|CEX|LP|PWA|DEMO|LIVE|"
    r"CRYPTO PULSE|Chrome|Android|Ubuntu|PRoot|proot|npm|pip|vite|Vite)\b"
)


def _visible_strings_from_python() -> list[tuple[str, str]]:
    """Every French-catalogue template, plus the labels the API exposes."""
    from cryptopulse.i18n import labels, reasons

    out: list[tuple[str, str]] = []
    for name in dir(reasons):
        if not name.isupper():
            continue
        entry = getattr(reasons, name)
        template = getattr(entry, "template", None)
        if isinstance(template, str):
            out.append((f"reasons.{name}", template))
    for table_name in (
        "SETUP_STATE_FR", "SETUP_STATE_LONG_FR", "ALERT_LEVEL_FR",
        "LIQUIDITY_FR", "TRADE_ACTION_FR", "STRENGTH_FR", "REGIME_FR",
    ):
        for key, value in getattr(labels, table_name).items():
            out.append((f"labels.{table_name}[{key}]", value))
    return out


def _looks_english(text: str) -> str | None:
    """The first English marker in `text`, or None. Acronyms are stripped first."""
    # `{bars}` is a placeholder name, not a word on screen — checking it would
    # flag the parameter rather than the sentence.
    cleaned = re.sub(r"\{[^}]*\}", " ", text)
    cleaned = ALLOWED.sub(" ", cleaned).lower()
    for loan in LOANWORDS:
        cleaned = cleaned.replace(loan, " ")
    for marker in ENGLISH_MARKERS:
        if marker in cleaned:
            return marker
    return None


# --------------------------------------------------------------------------- #
# The catalogue itself
# --------------------------------------------------------------------------- #


def test_every_catalogue_template_is_french():
    offenders = []
    for name, template in _visible_strings_from_python():
        marker = _looks_english(template)
        if marker:
            offenders.append(f"{name}: {template!r} contient {marker!r}")
    assert not offenders, "textes non traduits :\n  " + "\n  ".join(offenders)


def test_the_catalogue_covers_every_engine():
    """A module that stopped importing the catalogue would silently go back to
    literal strings, and nothing else here would notice."""
    from cryptopulse.i18n import reasons

    prefixes = {name.split("_")[0] for name in dir(reasons) if name.isupper()}
    for expected in ("RVOL", "STRUCTURE", "EXP", "DISC", "HUNT", "SAF", "LIQ", "PM", "CONF", "TRG"):
        assert expected in prefixes, f"aucune entrée pour {expected}"


# --------------------------------------------------------------------------- #
# The engines
# --------------------------------------------------------------------------- #

ENGINE_FILES = (
    "cryptopulse/scoring/components.py",
    "cryptopulse/scoring/discovery.py",
    "cryptopulse/scoring/explosion.py",
    "cryptopulse/scoring/pump_maturity.py",
    "cryptopulse/scoring/acceleration.py",
    "cryptopulse/scoring/confidence.py",
    "cryptopulse/scoring/states.py",
    "cryptopulse/risk/liquidity.py",
    "cryptopulse/risk/safety.py",
    "cryptopulse/hunter/discovery.py",
)


@pytest.mark.parametrize("path", ENGINE_FILES)
def test_no_engine_appends_a_literal_reason(path: str):
    """Reasons must come from the catalogue, not from a literal at the call
    site: a literal is how one untranslated sentence slips back in."""
    source = (ROOT / path).read_text(encoding="utf-8")
    literals = re.findall(
        r"(?:reasons|caveats|issues)\.append\(\s*f?[\"']", source
    )
    assert not literals, (
        f"{path} : {len(literals)} raison(s) écrite(s) en dur au lieu de passer "
        "par cryptopulse/i18n/reasons.py"
    )


# --------------------------------------------------------------------------- #
# The payloads the interface actually receives
# --------------------------------------------------------------------------- #


def test_a_real_scan_produces_only_french_reasons():
    """The end-to-end check: run the scanner and read what the screen would."""
    import asyncio

    from cryptopulse.config.settings import CryptoPulseSettings
    from cryptopulse.scanner.cex import CexScanner

    settings = CryptoPulseSettings()
    settings.providers.market_data = "fixture"
    settings.scanner.max_symbols = 6
    settings.scanner.min_quote_volume_24h = 100_000.0

    async def run():
        scanner = CexScanner(settings)
        report = await scanner.scan()
        await scanner.close()
        return report

    report = asyncio.run(run())
    assert report.results

    offenders = []
    for result in report.results:
        payload = result.to_dict()
        texts = list(payload["why"]) + list(payload["risks"])
        for key in ("trigger", "invalidation"):
            value = payload["setup"].get(key)
            if value:
                texts.append(value)
        for text in texts:
            marker = _looks_english(str(text))
            if marker:
                offenders.append(f"{result.symbol}: {text!r} contient {marker!r}")
    assert not offenders, "anglais dans un scan réel :\n  " + "\n  ".join(offenders[:10])


def test_setup_states_travel_with_a_french_label():
    """The identifier stays English on the wire; the label is what renders. A
    UI falling back to the value would print BREAKOUT on a French screen."""
    from cryptopulse.scoring.states import SetupState

    for state in SetupState:
        from cryptopulse.i18n.labels import SETUP_STATE_FR, SETUP_STATE_LONG_FR

        assert state.value in SETUP_STATE_FR
        assert state.value in SETUP_STATE_LONG_FR
        assert SETUP_STATE_LONG_FR[state.value] != state.value


def test_the_six_decisions_keep_their_specified_labels():
    """Spec §4 and §21, word for word."""
    from cryptopulse.i18n.labels import TRADE_ACTION_FR

    assert TRADE_ACTION_FR["BUY"] == "ACHETER"
    assert TRADE_ACTION_FR["HOLD"] == "CONSERVER"
    assert TRADE_ACTION_FR["WATCH"] == "SURVEILLER"
    assert TRADE_ACTION_FR["REDUCE"] == "RÉDUIRE / PROTÉGER"
    assert TRADE_ACTION_FR["SELL"] == "VENDRE"
    assert TRADE_ACTION_FR["AVOID"] == "NE PAS ACHETER"


# --------------------------------------------------------------------------- #
# French typography
# --------------------------------------------------------------------------- #


def test_numbers_use_french_conventions():
    from cryptopulse.i18n import money, mult, num, pct

    # The separator before % is U+202F, the narrow no-break space French
    # typography requires. A plain space would let the sign wrap to the next
    # line on a phone; a missing space is an anglicism.
    nbsp = "\u202f"
    assert num(2.59) == "2,59"
    assert pct(51.0) == f"+51{nbsp}%"
    assert pct(-3.2, 1) == f"-3,2{nbsp}%"
    assert mult(2.59) == "2,59×"
    assert num(1234567.5, 0) == f"1{nbsp}234{nbsp}568"
    assert "34" in money(34000.5) and "$" in money(34000.5)
    # A missing value is an em dash, never a zero.
    assert num(None) == "—" and pct(None) == "—" and money(None) == "—"


def test_no_catalogue_entry_uses_an_ascii_decimal_point_in_a_number():
    """`2.59` on a French screen is the tell that a value bypassed the
    formatters."""
    for name, template in _visible_strings_from_python():
        assert not re.search(r"\d\.\d", template), f"{name} : {template!r}"


# --------------------------------------------------------------------------- #
# The frontend
# --------------------------------------------------------------------------- #

FRONTEND = ROOT / "frontend" / "src"

# Headings and labels the user listed explicitly in §6 and §21.
FORBIDDEN_IN_JSX = (
    ">Why<", ">Why ", ">Risks<", ">Trigger<", ">Price<", ">Confidence<",
    ">Opportunity<", ">Maturity<", ">Safety<", ">Liquidity<", ">Status<",
    ">Win rate<", ">Success<", ">Median<", ">Average<", ">Best<", ">Worst<",
    ">Window<", ">Windows<", ">Scan now<", ">All<", ">Any<", ">State<",
)


@pytest.mark.parametrize("needle", FORBIDDEN_IN_JSX)
def test_no_english_heading_survives_in_the_interface(needle: str):
    offenders = [
        str(path.relative_to(ROOT))
        for path in FRONTEND.rglob("*.tsx")
        if needle in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"{needle} encore présent dans : {offenders}"


def test_the_interface_never_renders_a_raw_setup_state():
    """`{data.setup.state}` prints BREAKOUT. The label must be looked up."""
    for path in FRONTEND.rglob("*.tsx"):
        source = path.read_text(encoding="utf-8")
        assert "{data.setup.state}<" not in source, path.name


def test_the_frontend_catalogue_exists_and_has_no_english_fallback():
    """`labelFor` must not fall back to the identifier: "BREAKOUT" is readable
    enough that an untranslated value would go unnoticed for months."""
    source = (FRONTEND / "i18n" / "fr.ts").read_text(encoding="utf-8")
    assert "?? '—'" in source
    assert "?? key" not in source


# --------------------------------------------------------------------------- #
# Found by running the interface, not by reading it
# --------------------------------------------------------------------------- #


def test_no_component_renders_a_number_with_an_ascii_decimal_point():
    """`toFixed` writes `2.59`. Every displayed number must go through
    `format.ts`, which is the only place the comma is applied.

    Found by running the app: the score breakdown read `+0.6 / 20` and the
    penalty line `-5.0` while every reason beside them read `+3,42 %`. Two
    conventions on one screen is worse than either one alone.
    """
    # `format.ts` is where the conversion happens, so it is the one file that
    # must call toFixed. SVG path coordinates are geometry, never read.
    offenders = []
    for path in FRONTEND.rglob("*.tsx"):
        source = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(source.splitlines(), start=1):
            # `toPrecision` was the hole this test had: it writes `0.000420`
            # on a French screen and was invisible to a `toFixed`-only sweep.
            if ".toFixed(" not in line and ".toPrecision(" not in line:
                continue
            if "vectorEffect" in line or "`${i === 0 ? 'M' : 'L'}" in line:
                continue  # an SVG path, not a number on screen
            offenders.append(f"{path.name}:{line_no}")
    assert not offenders, (
        "toFixed/toPrecision contournent le formatage français, utilisez "
        "num/pct/price/mult/compact "
        f"depuis format.ts : {offenders}"
    )


def test_the_percent_sign_is_preceded_by_a_narrow_no_break_space():
    """French typography requires it, and *no-break* is the load-bearing half:
    a plain space lets `%` wrap to the next line on a phone."""
    from cryptopulse.i18n import pct

    assert pct(51.0).endswith(" %")
    for name, template in _visible_strings_from_python():
        for match in re.finditer(r"(.)%", template):
            before = match.group(1)
            # `{x}%` is fine — the parameter arrives already formatted by pct().
            assert before in (" ", "}", "%"), f"{name} : {template!r}"


def test_the_score_breakdown_labels_every_component_in_french():
    """The eight component keys are engine identifiers and stay English on the
    wire — the weights fingerprint is computed over them (invariant 13). What
    must not happen is the raw key reaching the screen."""
    source = (FRONTEND / "i18n" / "fr.ts").read_text(encoding="utf-8")
    for component in (
        "volume", "momentum", "structure", "breakout",
        "volatility", "orderflow", "mtf", "liquidity",
    ):
        assert f"  {component}:" in source, f"aucun libellé pour {component}"
    drawer = (FRONTEND / "components" / "AssetDrawer.tsx").read_text(encoding="utf-8")
    # The key prop is fine — React never renders it. What must not appear is the
    # raw identifier as text.
    assert ">{b.component}<" not in drawer, "la clé brute du composant est affichée"


def test_the_alert_body_the_user_reads_on_the_phone_is_french():
    """`format_text` is the notification and the webhook payload — the one alert
    surface read away from the screen (spec §17)."""
    from cryptopulse.alerts.engine import Alert, AlertLevel

    text = Alert(
        symbol="TESTUSDT", level=AlertLevel.HIGH, headline="CASSURE CONFIRMÉE",
        timestamp_ms=0, final_score=84.0, pump_maturity=10.0, data_confidence=90.0,
        safety=100.0, liquidity="GOOD", state="BREAKOUT", price=1.0,
        score_acceleration=5.0, why=["raison"], risks=["risque"],
    ).format_text()
    assert _looks_english(text) is None, text
    assert "POURQUOI" in text and "RISQUES" in text
    assert "BONNE" in text  # the liquidity label, not the enum value
