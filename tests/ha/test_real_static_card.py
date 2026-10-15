"""The card must be served BY Home Assistant, not fetched from GitHub.

v0.2.2 told users to point a Lovelace resource at raw.githubusercontent.com, which
answers `content-type: text/plain` with `x-content-type-options: nosniff`.  A
browser refuses to execute an ES module under those headers, so the element is
never defined and Lovelace shows `Custom element doesn't exist`.  These tests pin
the fix: the file ships inside the package, `async_setup` registers it at a
same-origin URL, and it is registered WITHOUT immutable cache headers so a HACS
upgrade is not pinned behind a year of caching.

`hass.http` is None in the test harness (the http integration does not bind a
port there), so the registration call is captured rather than served -- which is
also why `async_setup` must tolerate `hass.http` being absent.
"""

from __future__ import annotations

import pathlib

from custom_components.taipower_bill import CARD_URL, async_setup

DOMAIN_ROOT = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "taipower_bill"


class _RecordingHTTP:
    def __init__(self) -> None:
        self.configs: list = []

    async def async_register_static_paths(self, configs) -> None:
        self.configs.extend(configs)


async def test_card_file_ships_inside_the_integration(hass) -> None:
    """HACS copies only custom_components/<domain>/, so the asset must live there."""
    card = DOMAIN_ROOT / "www" / "taipower-bill-card.js"
    assert card.is_file(), f"{card} is missing -- HACS would install no card at all"
    body = card.read_text(encoding="utf-8")
    assert 'customElements.define("taipower-bill-card"' in body, "the element is not registered"
    assert "v0.2.3" in body, "the header version should say what the user has"


async def test_setup_registers_the_card_at_a_same_origin_url(hass, monkeypatch) -> None:
    fake = _RecordingHTTP()
    monkeypatch.setattr(hass, "http", fake, raising=False)
    assert await async_setup(hass, {})

    urls = [c.url_path for c in fake.configs]
    assert urls == [CARD_URL], f"expected exactly one registration at {CARD_URL}, got {urls}"
    assert CARD_URL == "/taipower-bill-card.js"
    assert not CARD_URL.startswith("http"), "a cross-origin URL is what broke v0.2.2"

    cfg = fake.configs[0]
    assert pathlib.Path(cfg.path).is_file(), "registered path does not exist on disk"
    assert cfg.cache_headers is False, "immutable caching would pin the old card after an upgrade"


async def test_setup_survives_a_missing_http_integration(hass, monkeypatch) -> None:
    """A headless instance must still set up -- it just cannot serve the card."""
    monkeypatch.setattr(hass, "http", None, raising=False)
    assert await async_setup(hass, {}) is True
