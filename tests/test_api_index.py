"""Coverage for the FastAPI adapter (api/index.py), especially the new
Team ID squad-loading route.

`_bootstrap()` is module-level-cached (`lru_cache`) so the app avoids
re-reading the release/database on every request in production -- tests
must clear that cache after pointing `FPL_DATABASE_PATH`/
`FPL_WEB_RELEASE_PATH` at a fresh fixture, or a later test would see an
earlier test's stale bootstrap.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

from api.index import _bootstrap, app
from fpl_model.ingest.fpl import FPLClient
from tests.test_webapp_service import _release_file

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_bootstrap_cache():
    _bootstrap.cache_clear()
    yield
    _bootstrap.cache_clear()


def _use_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs) -> None:
    release_path = tmp_path / "release.json"
    _release_file(release_path, **kwargs)
    monkeypatch.setenv("FPL_WEB_RELEASE_PATH", str(release_path))
    monkeypatch.setenv("FPL_DATABASE_PATH", str(tmp_path / "missing.duckdb"))


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


def test_health_reports_compact_release_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _use_release(tmp_path, monkeypatch)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["mode"] == "compact_release"


def test_bootstrap_returns_the_release_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _use_release(tmp_path, monkeypatch)

    response = client.get("/api/bootstrap")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["players"]) == 15
    assert payload["release"]["health"] == "shadow"


def test_squad_from_entry_resolves_a_live_picks_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _use_release(tmp_path, monkeypatch)
    monkeypatch.setattr(FPLClient, "entry_picks", lambda self, entry_id, gw: _picks_payload())

    response = client.get("/api/squad/from-entry/123456")

    assert response.status_code == 200
    payload = response.json()
    assert payload["entry_id"] == 123456
    assert payload["gameweek"] == 2  # the release's own start Gameweek
    assert sorted(payload["fpl_ids"]) == list(range(1, 16))
    assert payload["captain_fpl_id"] == 9
    assert payload["selling_price_is_estimated"] is True


def test_squad_from_entry_accepts_an_explicit_gameweek(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _use_release(tmp_path, monkeypatch)
    captured_gameweeks = []

    def fake_entry_picks(self, entry_id, gw):
        captured_gameweeks.append(gw)
        return _picks_payload()

    monkeypatch.setattr(FPLClient, "entry_picks", fake_entry_picks)

    response = client.get("/api/squad/from-entry/123456?gameweek=4")

    assert response.status_code == 200
    assert captured_gameweeks == [4]
    assert response.json()["gameweek"] == 4


def test_squad_from_entry_rejects_a_non_positive_entry_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _use_release(tmp_path, monkeypatch)

    response = client.get("/api/squad/from-entry/0")

    assert response.status_code == 422


def test_squad_from_entry_returns_404_when_fpl_has_no_picks_for_that_gameweek(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _use_release(tmp_path, monkeypatch)

    def raise_not_found(self, entry_id, gw):
        response = requests.Response()
        response.status_code = 404
        raise requests.HTTPError(response=response)

    monkeypatch.setattr(FPLClient, "entry_picks", raise_not_found)

    response = client.get("/api/squad/from-entry/123456")

    assert response.status_code == 404
    assert "123456" in response.json()["detail"]


def test_squad_from_entry_returns_502_on_fpl_network_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _use_release(tmp_path, monkeypatch)

    def raise_connection_error(self, entry_id, gw):
        raise requests.ConnectionError("network unreachable")

    monkeypatch.setattr(FPLClient, "entry_picks", raise_connection_error)

    response = client.get("/api/squad/from-entry/123456")

    assert response.status_code == 502


def test_squad_from_entry_rejects_a_player_outside_the_release_horizon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _use_release(tmp_path, monkeypatch)
    payload = _picks_payload()
    payload["picks"][0]["element"] = 9999
    monkeypatch.setattr(FPLClient, "entry_picks", lambda self, entry_id, gw: payload)

    response = client.get("/api/squad/from-entry/123456")

    assert response.status_code == 422


def test_squad_from_entry_never_writes_a_database_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _use_release(tmp_path, monkeypatch)
    database_path = tmp_path / "missing.duckdb"
    monkeypatch.setattr(FPLClient, "entry_picks", lambda self, entry_id, gw: _picks_payload())

    client.get("/api/squad/from-entry/123456")

    assert not database_path.exists()


def test_recommend_lineups_accepts_a_squad_resolved_from_an_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _use_release(tmp_path, monkeypatch)
    monkeypatch.setattr(FPLClient, "entry_picks", lambda self, entry_id, gw: _picks_payload())

    resolved = client.get("/api/squad/from-entry/123456").json()
    response = client.post(
        "/api/recommend/lineups",
        json={
            "fpl_ids": resolved["fpl_ids"],
            "bank_tenths": resolved["bank_tenths"],
            "free_transfers": 1,
            "selling_prices": resolved["selling_prices"],
        },
    )

    assert response.status_code == 200
    assert response.json()["horizon"] == [2, 3, 4]


def test_recommend_lineups_recomputes_from_a_role_scenario_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _use_release(tmp_path, monkeypatch)
    fpl_ids = list(range(1, 16))

    baseline = client.post(
        "/api/recommend/lineups", json={"fpl_ids": fpl_ids}
    ).json()
    scenario = client.post(
        "/api/recommend/lineups",
        json={
            "fpl_ids": fpl_ids,
            "role_scenario_overrides": [{"fpl_id": 11, "gameweek": 2, "xpts": 0.0}],
        },
    ).json()

    assert baseline["is_reviewed_scenario"] is False
    assert scenario["is_reviewed_scenario"] is True
    baseline_starters = {row["fpl_id"] for row in baseline["lineups"][0]["starters"]}
    scenario_starters = {row["fpl_id"] for row in scenario["lineups"][0]["starters"]}
    assert 11 in baseline_starters
    assert 11 not in scenario_starters


def test_recommend_lineups_rejects_an_out_of_horizon_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _use_release(tmp_path, monkeypatch)

    response = client.post(
        "/api/recommend/lineups",
        json={
            "fpl_ids": list(range(1, 16)),
            "role_scenario_overrides": [{"fpl_id": 11, "gameweek": 1, "xpts": 0.0}],
        },
    )

    assert response.status_code == 422


def test_recommend_lineups_rejects_a_negative_override_xpts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _use_release(tmp_path, monkeypatch)

    response = client.post(
        "/api/recommend/lineups",
        json={
            "fpl_ids": list(range(1, 16)),
            "role_scenario_overrides": [{"fpl_id": 11, "gameweek": 2, "xpts": -1.0}],
        },
    )

    # Rejected by Pydantic field validation (ge=0.0) before reaching the
    # service layer, so this is a 422 from FastAPI's own request validation.
    assert response.status_code == 422
