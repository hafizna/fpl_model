"""End-to-end coverage: FPL Team ID to a rendered weekly lineup, no CLI.

Drives the actual served page in a real (headless) browser -- not just the
FastAPI TestClient -- so this exercises app.js/the DOM the way P1's roadmap
item names explicitly ("end-to-end tests for Team ID to weekly decision
without a CLI"), on top of the API-level coverage in test_api_index.py.

Runs a real uvicorn server in a background thread (Playwright drives pages
over real HTTP; it cannot talk to an ASGI app in-process the way
`TestClient` can) against a fixture compact release, with `FPLClient.entry_picks`
monkeypatched so no request reaches the real FPL API.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from api.index import _bootstrap, app
from fpl_model.ingest.fpl import FPLClient
from tests.test_webapp_service import _release_file


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _picks_payload(*, bank: int = 5, captain_fpl_id: int = 9, vice_fpl_id: int = 5) -> dict:
    return {
        "active_chip": None,
        "entry_history": {"event": 2, "bank": bank, "value": 1005},
        "picks": [
            {
                "element": fpl_id,
                "position": fpl_id,
                "multiplier": 2 if fpl_id == captain_fpl_id else 1,
                "is_captain": fpl_id == captain_fpl_id,
                "is_vice_captain": fpl_id == vice_fpl_id,
                "element_type": 1,
            }
            for fpl_id in range(1, 16)
        ],
    }


@pytest.fixture
def live_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Serve the real app over real HTTP, backed by a fixture compact release."""
    release_path = tmp_path / "release.json"
    _release_file(release_path)
    monkeypatch.setenv("FPL_WEB_RELEASE_PATH", str(release_path))
    monkeypatch.setenv("FPL_DATABASE_PATH", str(tmp_path / "missing.duckdb"))
    monkeypatch.setattr(FPLClient, "entry_picks", lambda self, entry_id, gw: _picks_payload())
    _bootstrap.cache_clear()

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("uvicorn server did not start in time")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        _bootstrap.cache_clear()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        yield instance
        instance.close()


def _seed_local_storage_squad(page) -> None:
    """Pre-seed a squad matching the fixture release's own 15 players
    (fpl_id 1-15) so the page's own initial-load lineup call succeeds without
    depending on app.js's hardcoded DEFAULT_SQUAD, which names real
    production fpl_ids the tiny fixture release does not contain."""
    page.add_init_script(
        "localStorage.setItem('touchline-squad', JSON.stringify("
        + str(list(range(1, 16)))
        + "))"
    )


def test_team_id_loads_a_squad_and_renders_a_weekly_lineup(live_server, browser):
    page = browser.new_page()
    try:
        _seed_local_storage_squad(page)
        page.goto(live_server, wait_until="networkidle", timeout=15000)
        page.wait_for_selector("#pitch:not(.skeleton)", timeout=15000)

        page.fill("#team-id", "123456")
        page.click("#load-team-id")
        page.wait_for_function(
            "document.getElementById('load-team-id').textContent === 'Load squad'",
            timeout=15000,
        )

        assert page.is_hidden("#error-banner")
        page.wait_for_selector("#pitch .player-card", timeout=15000)

        starters = page.locator("#pitch .player-card").count()
        assert starters == 11

        # fpl_id 9 is the fixture's captain (see _picks_payload); confirm the
        # rendered pitch actually marks exactly one starter "C" and one "V".
        assert page.locator("#pitch .captain-badge", has_text="C").count() == 1
        assert page.locator("#pitch .captain-badge", has_text="V").count() == 1

        summary_text = page.inner_text("#weekly-summary")
        assert "GW2 xPts" in summary_text
    finally:
        page.close()


def test_team_id_squad_flows_into_the_outlook_and_transfers_menus(live_server, browser):
    page = browser.new_page()
    try:
        _seed_local_storage_squad(page)
        page.goto(live_server, wait_until="networkidle", timeout=15000)
        page.wait_for_selector("#pitch:not(.skeleton)", timeout=15000)
        page.fill("#team-id", "123456")
        page.click("#load-team-id")
        page.wait_for_function(
            "document.getElementById('load-team-id').textContent === 'Load squad'",
            timeout=15000,
        )

        page.click("[data-view=outlook]")
        page.wait_for_selector("#outlook-total:not(.skeleton)", timeout=15000)
        outlook_text = page.inner_text("#outlook-total")
        assert "Projected horizon score" in outlook_text

        # Each of the 15 squad slots renders its own <select data-player-id="...">
        # with the currently-selected player as its team-chip label.
        team_chips = page.locator("#squad-editor .squad-player .team-chip")
        assert team_chips.count() == 15
    finally:
        page.close()


def test_an_invalid_team_id_surfaces_a_visible_error_not_a_silent_failure(live_server, browser):
    page = browser.new_page()
    try:
        _seed_local_storage_squad(page)
        page.goto(live_server, wait_until="networkidle", timeout=15000)
        page.wait_for_selector("#pitch:not(.skeleton)", timeout=15000)

        page.fill("#team-id", "0")
        page.click("#load-team-id")
        page.wait_for_function(
            "document.getElementById('error-banner').hidden === false", timeout=5000
        )

        assert "valid" in page.inner_text("#error-banner").lower()
    finally:
        page.close()
