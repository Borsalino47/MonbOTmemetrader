"""The PWA surface: is the app actually installable, and does the cache lie?

Two things decide whether this works at all, and both fail silently when wrong.
A service worker delivered with the wrong content type is rejected by the
browser with no error the user ever sees — the app just never becomes
installable. And a manifest missing one required field produces the same
non-event.

The third test is the one that matters for correctness rather than packaging:
the service worker must never cache /api. Serving a cached scan would put
yesterday's prices on screen with today's styling, which is precisely the
failure this project is built to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cryptopulse.api.app import FRONTEND_DIST, create_app
from cryptopulse.api.service import ScannerService, set_service
from cryptopulse.config.settings import CryptoPulseSettings
from cryptopulse.database.session import reset_engine

PUBLIC = Path(__file__).resolve().parents[1] / "frontend" / "public"
built = pytest.mark.skipif(
    not (FRONTEND_DIST / "index.html").is_file(),
    reason="frontend not built; run npm run build",
)


@pytest.fixture()
def client(tmp_path):
    reset_engine()
    s = CryptoPulseSettings()
    s.providers.market_data = "fixture"
    s.database.url = f"sqlite:///{tmp_path}/pwa.db"
    set_service(ScannerService(s))
    with TestClient(create_app(start_loop=False)) as c:
        yield c
    set_service(None)
    reset_engine()


# --------------------------------------------------------------------------- #
# The manifest
# --------------------------------------------------------------------------- #


def test_the_manifest_has_everything_chrome_requires_to_offer_installation():
    """Any one of these missing and the install option never appears, with no
    message anywhere explaining why."""
    m = json.loads((PUBLIC / "manifest.webmanifest").read_text())

    assert m["name"] and m["short_name"]
    assert len(m["short_name"]) <= 12, "a long short_name is truncated under the icon"
    assert m["start_url"] and m["scope"]
    assert m["display"] == "standalone", "anything else keeps the browser chrome"
    assert m["background_color"] and m["theme_color"]

    sizes = {i["sizes"] for i in m["icons"]}
    assert "192x192" in sizes and "512x512" in sizes, "both sizes are required"


def test_a_maskable_icon_exists_so_android_does_not_use_a_white_plate():
    m = json.loads((PUBLIC / "manifest.webmanifest").read_text())
    purposes = {i.get("purpose") for i in m["icons"]}
    assert "maskable" in purposes
    assert "any" in purposes, "a maskable-only set is cropped where it should not be"


def test_the_icons_referenced_by_the_manifest_actually_exist():
    m = json.loads((PUBLIC / "manifest.webmanifest").read_text())
    for icon in m["icons"]:
        path = PUBLIC / icon["src"].lstrip("/")
        assert path.is_file(), f"{icon['src']} is referenced but missing"
        # A PNG signature, so a placeholder text file cannot pass.
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_the_theme_colour_matches_the_application_background():
    """The status bar and the splash screen are painted from these before any
    CSS loads; a mismatch shows as a flash of the wrong colour on every launch."""
    m = json.loads((PUBLIC / "manifest.webmanifest").read_text())
    assert m["background_color"].lower() == "#0a0d12"
    assert m["theme_color"].lower() == "#0a0d12"
    assert '#0a0d12' in (PUBLIC.parent / "index.html").read_text()


# --------------------------------------------------------------------------- #
# The service worker must not cache live data
# --------------------------------------------------------------------------- #


def test_the_service_worker_never_caches_the_api():
    """The rule the whole project depends on. A cached /api response would put
    an old price on screen with no indication that it is old."""
    sw = (PUBLIC / "sw.js").read_text()

    assert "/api/" in sw, "the worker must explicitly recognise API requests"
    # The bail-out must come before any caching logic runs.
    api_check = sw.index("startsWith('/api/')")
    first_put = sw.index("cache.put")
    assert api_check < first_put, "API requests are handled after caching begins"

    guard = sw[api_check : api_check + 120]
    assert "return" in guard, "the API branch must return without caching"


def test_the_worker_caches_only_build_artifacts():
    sw = (PUBLIC / "sw.js").read_text()
    assert "response.type === 'basic'" in sw, "cross-origin responses must not be stored"
    assert "request.method !== 'GET'" in sw, "only GET is cacheable"


# --------------------------------------------------------------------------- #
# Serving
# --------------------------------------------------------------------------- #


@built
def test_the_service_worker_is_served_as_javascript_not_html(client):
    """The SPA catch-all returns index.html for unknown paths. If /sw.js fell
    through to it the browser would reject the worker silently."""
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert r.headers.get("service-worker-allowed") == "/"
    assert "no-cache" in r.headers.get("cache-control", "")
    assert "<!doctype html>" not in r.text.lower()


@built
def test_the_manifest_is_served_with_its_own_media_type(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert "manifest" in r.headers["content-type"]
    assert json.loads(r.text)["short_name"] == "Crypto Pulse"


@built
def test_icons_are_served_as_images(client):
    for path in ("/icons/icon-192.png", "/icons/icon-512.png", "/icons/icon-maskable-512.png"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


@built
def test_the_html_links_the_manifest_and_the_ios_icon(client):
    html = client.get("/").text
    assert 'rel="manifest"' in html
    assert "manifest.webmanifest" in html
    # iOS ignores the manifest and reads this tag instead.
    assert 'rel="apple-touch-icon"' in html
    assert 'name="theme-color"' in html
    assert "viewport-fit=cover" in html, "without this the app cannot paint under the notch"


@built
def test_the_api_still_answers_normally_alongside_the_pwa_routes(client):
    """The PWA routes are registered before the SPA catch-all; this checks they
    did not shadow anything."""
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/config").status_code == 200
