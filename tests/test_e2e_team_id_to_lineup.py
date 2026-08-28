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

import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from api.index import ALPHA_RATE_LIMITER, _bootstrap, app
from fpl_model.ingest.fpl import FPLClient
from fpl_model.webapp.alpha_access import hash_access_token
from tests.test_webapp_service import _ready_rating_artifact, _release_file


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _picks_payload(*, bank: int = 5, captain_fpl_id: int = 9, vice_fpl_id: int = 5) -> dict:
    pick_order = (1, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14, 2, 6, 7, 15)
    return {
        "active_chip": None,
        "entry_history": {"event": 2, "bank": bank, "value": 1005},
        "picks": [
            {
                "element": fpl_id,
                "position": position,
                "multiplier": 2 if fpl_id == captain_fpl_id else 1,
                "is_captain": fpl_id == captain_fpl_id,
                "is_vice_captain": fpl_id == vice_fpl_id,
                "element_type": 1,
            }
            for position, fpl_id in enumerate(pick_order, start=1)
        ],
    }


@pytest.fixture
def live_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Serve the real app over real HTTP, backed by a fixture compact release."""
    release_path = tmp_path / "release.json"
    _release_file(release_path)
    payload = json.loads(release_path.read_text(encoding="utf-8"))
    for fpl_id, position in zip(range(16, 20), ("GK", "DEF", "MID", "FWD"), strict=True):
        payload["players"].append(
            {
                "fpl_id": fpl_id,
                "player_code": 10_000 + fpl_id,
                "name": f"Candidate {fpl_id}",
                "team_id": 6,
                "team": "T6",
                "position": position,
                "price_tenths": 50,
                "status": "a",
                "gameweeks": {
                    str(gameweek): {
                        "xpts": 8.0,
                        "appearance_probability": 0.95,
                        "uncertainty": None,
                        "quality_flags": [],
                    }
                    for gameweek in (2, 3, 4)
                },
            }
        )
    payload["release"]["rating_benchmark"] = _ready_rating_artifact()
    release_path.write_text(json.dumps(payload), encoding="utf-8")
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


@pytest.fixture
def gated_live_server(live_server, monkeypatch: pytest.MonkeyPatch):
    token = "browser-alpha-code-123456"
    monkeypatch.setenv("FPL_REQUIRE_ALPHA_ACCESS", "true")
    monkeypatch.setenv(
        "FPL_ALPHA_ACCESS_TOKEN_HASHES", f"browser={hash_access_token(token)}"
    )
    ALPHA_RATE_LIMITER.clear()
    try:
        yield live_server, token
    finally:
        ALPHA_RATE_LIMITER.clear()


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


def test_closed_alpha_prompt_unlocks_the_workspace_for_one_browser_session(
    gated_live_server, browser
):
    base_url, token = gated_live_server
    page = browser.new_page()
    try:
        _seed_local_storage_squad(page)
        page.goto(base_url, wait_until="networkidle", timeout=15000)
        page.wait_for_selector("#access-gate", state="visible", timeout=5000)

        assert page.locator("#pitch").get_attribute("class") == "pitch skeleton"
        page.fill("#access-code", token)
        page.click("#access-submit")
        page.wait_for_selector("#access-gate", state="hidden", timeout=15000)
        page.wait_for_selector("#pitch:not(.skeleton)", timeout=15000)

        assert page.evaluate("sessionStorage.getItem('touchline-alpha-token')") == token
        assert page.evaluate("localStorage.getItem('touchline-alpha-token')") is None
        assert token not in page.url
    finally:
        page.close()


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

        # The optimized result must still render exactly one captain and one
        # vice-captain after comparing against the submitted picks.
        assert page.locator("#pitch .captain-badge", has_text="C").count() == 1
        assert page.locator("#pitch .captain-badge", has_text="V").count() == 1

        summary_text = page.inner_text("#weekly-summary")
        assert "GW2 xPts" in summary_text
        assert "pct" in summary_text
        marginal_text = page.inner_text("#marginal-changes")
        assert "changes vs your current setup" in marginal_text.lower()
        assert "+10.00 xPts" in marginal_text
        assert "Player 9 → Player 15" in marginal_text
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
        assert "Model Preview" in outlook_text
        assert "percentile" in outlook_text

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


def test_transfer_scan_labels_free_and_hit_modes_before_suggestions(live_server, browser):
    page = browser.new_page()
    try:
        _seed_local_storage_squad(page)
        page.goto(live_server, wait_until="networkidle", timeout=15000)
        page.wait_for_selector("#pitch:not(.skeleton)", timeout=15000)
        page.click("[data-view=transfers]")

        page.click("#run-transfers")
        page.wait_for_selector("#transfer-results .transfer-mode.free", timeout=30000)
        assert "Free transfer available" in page.inner_text("#transfer-results .transfer-mode")
        assert "no hit" in page.inner_text("#transfer-results")
        assert "Model Preview" in page.inner_text("#transfer-results")

        page.select_option("#free-transfers", "0")
        page.click("#run-transfers")
        page.wait_for_selector("#transfer-results .transfer-mode.hit", timeout=30000)
        assert "Hit scenario" in page.inner_text("#transfer-results .transfer-mode")
        assert "4-point hit" in page.inner_text("#transfer-results .transfer-mode")
    finally:
        page.close()
