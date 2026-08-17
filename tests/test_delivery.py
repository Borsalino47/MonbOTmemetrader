"""Alert delivery: does a notification leak its own credential, and can it break a scan?

A webhook URL is a bearer token in a string. The tests that matter here are less
about formatting and more about what never escapes: the URL must not appear in a
report, a log line, an exception, or an API response — and a dead webhook must
cost a notification, never a scan.
"""

from __future__ import annotations

import httpx

from cryptopulse.alerts.delivery import (
    AlertDelivery,
    DeliveryReport,
    redacted,
    send_alerts,
    webhook_flavour,
)
from cryptopulse.alerts.engine import Alert, AlertLevel

SECRET = "https://discord.com/api/webhooks/123456789/AbCdEf-super-secret-token"


def _alert(symbol: str = "SOLUSDT", level: AlertLevel = AlertLevel.HIGH) -> Alert:
    return Alert(
        symbol=symbol, level=level, headline="breakout on expanding volume",
        timestamp_ms=1_700_000_000_000, final_score=84.0, pump_maturity=22.0,
        data_confidence=91.0, safety=88.0, liquidity="GOOD", state="BREAKOUT",
        price=178.42, score_acceleration=7.5,
        why=["RVOL 3.1x", "level cleared on a closed candle", "bid-side depth dominant"],
        risks=["RSI 74 stretched"], trigger=None, invalidation="a close back under 175.10",
        dedup_key="abc123",
    )


# --------------------------------------------------------------------------- #
# The URL is a credential
# --------------------------------------------------------------------------- #


def test_redaction_keeps_the_host_and_drops_the_token():
    out = redacted(SECRET)
    assert "discord.com" in out, "the host must survive, or a misconfiguration is undiagnosable"
    assert "super-secret-token" not in out
    assert "123456789" not in out


def test_redaction_handles_nonsense_without_raising():
    assert redacted(None) is None
    assert redacted("not a url") == "<unparseable url>"


async def test_a_failed_delivery_report_never_contains_the_url():
    """The obvious implementation puts str(exc) in the report — and httpx embeds
    the request URL in its exception messages."""
    delivery = AlertDelivery(webhook_url="https://127.0.0.1:9/hooks/secret-token-here", timeout=0.4)
    report = await delivery.send([_alert()])

    assert report.ok is False
    blob = str(report.to_dict())
    assert "secret-token-here" not in blob
    assert "127.0.0.1" in blob, "the host is kept so the failure can be diagnosed"


async def test_a_rejected_delivery_report_never_contains_the_url():
    delivery = AlertDelivery(webhook_url=SECRET)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "forbidden"})

    await _send_via(delivery, [_alert()], handler)
    blob = str(delivery.last.to_dict())
    assert "super-secret-token" not in blob
    assert delivery.last.status_code == 403
    assert delivery.last.ok is False


# --------------------------------------------------------------------------- #
# A failure is a failure, not silence — and never a lost scan
# --------------------------------------------------------------------------- #


async def test_an_unreachable_webhook_returns_a_report_instead_of_raising():
    delivery = AlertDelivery(webhook_url="https://127.0.0.1:9/nope", timeout=0.4)
    report = await send_alerts(delivery, [_alert()])
    assert isinstance(report, DeliveryReport)
    assert report.attempted is True
    assert report.ok is False
    assert report.error, "a failure must state itself"


async def test_no_webhook_configured_is_a_no_op_not_an_error():
    delivery = AlertDelivery(webhook_url=None)
    report = await delivery.send([_alert()])
    assert delivery.configured is False
    assert report.attempted is False
    assert report.error is None, "not configured is not a failure"


async def test_no_alerts_is_a_no_op():
    delivery = AlertDelivery(webhook_url=SECRET)
    report = await delivery.send([])
    assert report.attempted is False


# --------------------------------------------------------------------------- #
# What actually goes out
# --------------------------------------------------------------------------- #


def test_the_payload_shape_is_inferred_from_the_host():
    assert webhook_flavour(SECRET) == "discord"
    assert webhook_flavour("https://hooks.slack.com/services/T/B/X") == "slack"
    assert webhook_flavour("https://my-own-server.example/hook") == "generic"


async def test_a_score_is_never_sent_as_a_percentage():
    """A notification is read with no surrounding context, so this is the one
    place a bare '84%' would do the most damage."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(204)

    await _send_via(AlertDelivery(webhook_url=SECRET), [_alert()], handler)
    assert "84/100" in captured["body"]
    assert "84%" not in captured["body"]


async def test_the_disclaimer_travels_with_every_message():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(204)

    await _send_via(AlertDelivery(webhook_url=SECRET), [_alert()], handler)
    assert "pas des probabilités" in captured["body"]
    assert "ne passe jamais aucun ordre" in captured["body"]


async def test_a_demo_alert_is_marked_before_anything_actionable():
    """A synthetic alert that looks live is worse than no alert at all."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(204)

    await _send_via(AlertDelivery(webhook_url=SECRET, synthetic=True), [_alert()], handler)
    body = captured["body"]
    assert "SYNTHÉTIQUES" in body
    # The warning must come before the first symbol, not as a footnote.
    assert body.index("SYNTHÉTIQUES") < body.index("SOLUSDT")


async def test_a_generic_webhook_receives_the_structured_alert():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["json"] = json.loads(request.content)
        return httpx.Response(200)

    await _send_via(AlertDelivery(webhook_url="https://example.test/hook"), [_alert()], handler)
    body = captured["json"]
    assert body["count"] == 1
    assert body["alerts"][0]["symbol"] == "SOLUSDT"
    assert body["alerts"][0]["opportunity_score"] == "84/100"
    assert body["synthetic_data"] is False
    assert body["disclaimer"]


async def test_a_burst_of_alerts_is_capped_so_the_message_stays_readable():
    from cryptopulse.alerts.delivery import MAX_ALERTS_PER_MESSAGE

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(204)

    alerts = [_alert(symbol=f"SYM{i}USDT") for i in range(12)]
    report = await _send_via(AlertDelivery(webhook_url=SECRET), alerts, handler)
    assert report.delivered == MAX_ALERTS_PER_MESSAGE
    assert "SYM11USDT" not in captured["body"]


async def test_a_successful_delivery_reports_how_many_went_out():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    report = await _send_via(AlertDelivery(webhook_url=SECRET), [_alert(), _alert("BTCUSDT")], handler)
    assert report.ok is True
    assert report.delivered == 2
    assert report.status_code == 200


# --------------------------------------------------------------------------- #


async def _send_via(delivery: AlertDelivery, alerts, handler) -> DeliveryReport:
    """Run `delivery.send` against a mock transport instead of the network."""
    import httpx as _httpx

    real = _httpx.AsyncClient
    transport = _httpx.MockTransport(handler)

    class Patched(real):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    _httpx.AsyncClient = Patched
    try:
        return await delivery.send(alerts)
    finally:
        _httpx.AsyncClient = real
