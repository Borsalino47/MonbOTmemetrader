"""Alert delivery.

Two properties matter more than the payload format:

* a broken channel must never break the scan that produced the alert;
* a secret must never reach a log, an error string, or a returned detail.

Both are tested here, because both fail silently in production if they regress.
"""

from __future__ import annotations

import json

import httpx

from cryptopulse.alerts.engine import Alert, AlertKind, AlertLevel
from cryptopulse.alerts.notifiers import (
    DiscordNotifier,
    JsonlNotifier,
    Notifier,
    NotifierHub,
    TelegramNotifier,
    WebhookNotifier,
)
from cryptopulse.config.settings import AlertSettings

# asyncio_mode = "auto" (pyproject) picks up the async tests; the sync ones here
# must not be marked, or pytest-asyncio warns about them.

SECRET_TOKEN = "123456:SUPERSECRETBOTTOKEN"
SECRET_URL = "https://hooks.example.com/services/T000/B111/SECRETPATH"


def _alert(symbol: str = "PEPEUSDT") -> Alert:
    return Alert(
        symbol=symbol,
        level=AlertLevel.HIGH,
        headline="MOONSHOT — IGNITION ON A LONG BASE",
        timestamp_ms=1_700_000_000_000,
        final_score=71.0,
        pump_maturity=22.0,
        data_confidence=88.0,
        safety=90.0,
        liquidity="GOOD",
        state="ARMED",
        price=0.0000081,
        score_acceleration=4.5,
        why=["120 1d bars inside one range"],
        risks=["market cap unknown"],
        dedup_key="abc123",
        kind=AlertKind.MOONSHOT,
        moonshot_score=77.0,
        moonshot_stage="IGNITION",
        moonshot_multiple=8.4,
    )


def _capture(store: list[httpx.Request], status: int = 200):
    async def handler(request: httpx.Request) -> httpx.Response:
        store.append(request)
        return httpx.Response(status, json={"ok": status < 400})

    return handler


def _patch_transport(monkeypatch, handler) -> None:
    """Route every httpx.AsyncClient the notifiers create through a mock."""
    original = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)


# --------------------------------------------------------------------------- #
# Local channels
# --------------------------------------------------------------------------- #


async def test_jsonl_appends_one_parsable_object_per_alert(tmp_path):
    path = tmp_path / "nested" / "alerts.jsonl"
    notifier = JsonlNotifier(str(path))

    assert (await notifier.send([_alert("AAA"), _alert("BBB")])).delivered == 2
    assert (await notifier.send([_alert("CCC")])).delivered == 1

    lines = path.read_text().strip().split("\n")
    assert [json.loads(line)["symbol"] for line in lines] == ["AAA", "BBB", "CCC"]
    assert json.loads(lines[0])["kind"] == "MOONSHOT"
    assert json.loads(lines[0])["moonshot_score"] == 77.0


async def test_a_jsonl_path_that_cannot_be_written_reports_failure_without_raising(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    result = await JsonlNotifier(str(blocker / "alerts.jsonl")).send([_alert()])
    assert result.delivered == 0
    assert result.failed == 1


# --------------------------------------------------------------------------- #
# Network channels
# --------------------------------------------------------------------------- #


async def test_webhook_posts_the_whole_alert_so_a_receiver_can_route_on_it(monkeypatch):
    seen: list[httpx.Request] = []
    _patch_transport(monkeypatch, _capture(seen))

    result = await WebhookNotifier(SECRET_URL, 5.0).send([_alert()])
    assert result.delivered == 1
    body = json.loads(seen[0].content)
    assert body["alert"]["symbol"] == "PEPEUSDT"
    assert body["alert"]["moonshot_stage"] == "IGNITION"


async def test_telegram_sends_the_rendered_text_to_the_configured_chat(monkeypatch):
    seen: list[httpx.Request] = []
    _patch_transport(monkeypatch, _capture(seen))

    result = await TelegramNotifier(SECRET_TOKEN, "-100123", 5.0).send([_alert()])
    assert result.delivered == 1
    body = json.loads(seen[0].content)
    assert body["chat_id"] == "-100123"
    assert "MOONSHOT" in body["text"]
    assert "Moonshot Score: 77/100" in body["text"]


async def test_discord_wraps_the_text_and_stays_inside_the_platform_limit(monkeypatch):
    seen: list[httpx.Request] = []
    _patch_transport(monkeypatch, _capture(seen))

    await DiscordNotifier(SECRET_URL, 5.0).send([_alert()])
    content = json.loads(seen[0].content)["content"]
    assert content.startswith("```") and len(content) <= 2000


async def test_a_failing_endpoint_is_counted_and_never_raises(monkeypatch):
    _patch_transport(monkeypatch, _capture([], status=500))
    result = await WebhookNotifier(SECRET_URL, 5.0).send([_alert(), _alert()])
    assert result.delivered == 0
    assert result.failed == 2
    assert "500" in (result.detail or "")


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #


async def test_no_failure_detail_ever_contains_a_token_or_a_webhook_url(monkeypatch):
    """The token is in the URL path — a leaked detail string leaks the account."""
    async def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to " + str(request.url))

    _patch_transport(monkeypatch, boom)

    for notifier in (
        TelegramNotifier(SECRET_TOKEN, "-100123", 1.0),
        WebhookNotifier(SECRET_URL, 1.0),
        DiscordNotifier(SECRET_URL, 1.0),
    ):
        result = await notifier.send([_alert()])
        rendered = json.dumps(result.to_dict())
        assert result.failed == 1
        assert SECRET_TOKEN not in rendered
        assert "SECRETPATH" not in rendered
        assert "hooks.example.com" not in rendered


def test_an_unconfigured_channel_names_the_setting_and_not_a_value():
    hub = NotifierHub.from_settings(
        AlertSettings(channels=["telegram", "discord", "webhook"], telegram_bot_token=SECRET_TOKEN)
    )
    described = {c["channel"]: c for c in hub.describe()}
    assert described["telegram"]["configured"] is False
    assert described["telegram"]["missing_setting"] == "CP_ALERT_TELEGRAM_CHAT_ID"
    assert described["discord"]["missing_setting"] == "CP_ALERT_DISCORD_WEBHOOK_URL"
    assert SECRET_TOKEN not in json.dumps(hub.describe())


# --------------------------------------------------------------------------- #
# Fan-out
# --------------------------------------------------------------------------- #


async def test_one_crashing_channel_does_not_stop_the_others(tmp_path):
    class Exploding(Notifier):
        name = "exploding"

        async def send(self, alerts):
            raise RuntimeError("boom")

    path = tmp_path / "alerts.jsonl"
    hub = NotifierHub([Exploding(), JsonlNotifier(str(path))])
    results = {r.channel: r for r in await hub.dispatch([_alert()])}

    assert results["exploding"].failed == 1
    assert "crashed" in results["exploding"].detail
    assert results["jsonl"].delivered == 1
    assert path.exists()


async def test_dispatching_nothing_is_a_no_op():
    hub = NotifierHub.from_settings(AlertSettings(channels=["console"]))
    assert await hub.dispatch([]) == []


def test_an_unknown_channel_name_is_ignored_rather_than_crashing_startup():
    hub = NotifierHub.from_settings(AlertSettings(channels=["console", "carrier-pigeon"]))
    assert [c["channel"] for c in hub.describe()] == ["console"]
