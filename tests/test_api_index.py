"""Coverage for the FastAPI adapter (api/index.py), especially the new
Team ID squad-loading route.

`_bootstrap()` is module-level-cached (`lru_cache`) so the app avoids
re-reading the release/database on every request in production -- tests
must clear that cache after pointing `FPL_DATABASE_PATH`/
`FPL_WEB_RELEASE_PATH` at a fresh fixture, or a later test would see an
earlier test's stale bootstrap.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

from api.index import _allowed_release_health, _bootstrap, app
from fpl_model.ingest.fpl import FPLClient
from tests.test_webapp_service import _ready_rating_artifact, _release_file

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


def test_health_reports_compact_release_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _use_release(tmp_path, monkeypatch)

    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "compact_release"
    assert payload["ready"] is True
    assert payload["release_health"] == "shadow"
    assert payload["horizon"] == [2, 3, 4]
    assert payload["catalog_players"] == 15


def test_liveness_does_not_require_loading_the_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    missing = tmp_path / "missing.json"
    monkeypatch.setenv("FPL_WEB_RELEASE_PATH", str(missing))
    monkeypatch.setenv("FPL_DATABASE_PATH", str(tmp_path / "missing.duckdb"))

    response = client.get("/api/live")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "service": "fpl-web-api"}


def test_readiness_fails_when_the_release_cannot_be_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    broken_release = tmp_path / "broken.json"
    broken_release.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("FPL_WEB_RELEASE_PATH", str(broken_release))
    monkeypatch.setenv("FPL_DATABASE_PATH", str(tmp_path / "missing.duckdb"))

    response = client.get("/api/ready")

    assert response.status_code == 503
    assert "not ready" in response.json()["detail"]


def test_readiness_fails_closed_when_release_health_is_not_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _use_release(tmp_path, monkeypatch)
    monkeypatch.setenv("FPL_ALLOWED_RELEASE_HEALTH", "production")

    response = client.get("/api/ready")

    assert response.status_code == 503
    assert "release health 'shadow' is not allowed" in response.json()["detail"]


def test_production_readiness_requires_materialized_rating_benchmark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    release_path = tmp_path / "production.json"
    _release_file(release_path)
    payload = json.loads(release_path.read_text(encoding="utf-8"))
    payload["release"]["health"] = "production"
    release_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("FPL_WEB_RELEASE_PATH", str(release_path))
    monkeypatch.setenv("FPL_DATABASE_PATH", str(tmp_path / "missing.duckdb"))

    response = client.get("/api/ready")

    assert response.status_code == 503
    assert "materialized squad benchmark" in response.json()["detail"]


def test_production_readiness_exposes_materialized_benchmark_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    release_path = tmp_path / "production.json"
    _release_file(release_path)
    payload = json.loads(release_path.read_text(encoding="utf-8"))
    payload["release"]["health"] = "production"
    payload["release"]["rating_benchmark"] = _ready_rating_artifact()
    release_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("FPL_WEB_RELEASE_PATH", str(release_path))
    monkeypatch.setenv("FPL_DATABASE_PATH", str(tmp_path / "missing.duckdb"))

    response = client.get("/api/ready")

    assert response.status_code == 200
    assert response.json()["rating_benchmark_status"] == "ready"
    assert response.json()["rating_benchmark_id"] == "squad_benchmark_master_test"


def test_vercel_default_never_allows_a_research_release(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FPL_ALLOWED_RELEASE_HEALTH", raising=False)
    monkeypatch.setenv("VERCEL", "1")

    assert _allowed_release_health() == {"shadow", "production"}


def test_every_response_has_a_traceable_request_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _use_release(tmp_path, monkeypatch)

    generated = client.get("/api/live")
    supplied = client.get("/api/health", headers={"X-Request-ID": "alpha-smoke-1"})

    assert len(generated.headers["X-Request-ID"]) == 32
    assert supplied.headers["X-Request-ID"] == "alpha-smoke-1"


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


def test_request_log_uses_route_template_instead_of_private_team_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    _use_release(tmp_path, monkeypatch)
    monkeypatch.setattr(FPLClient, "entry_picks", lambda self, entry_id, gw: _picks_payload())

    with caplog.at_level(logging.INFO, logger="fpl_model.web"):
        response = client.get("/api/squad/from-entry/123456")

    assert response.status_code == 200
    request_logs = [record.message for record in caplog.records if '"event":"http_request"' in record.message]
    assert any('"route":"/api/squad/from-entry/{entry_id}"' in row for row in request_logs)
    assert all("123456" not in row for row in request_logs)


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
            "current_setup": {
                "gameweek": resolved["gameweek"],
                "starter_fpl_ids": resolved["starter_fpl_ids"],
                "bench_fpl_ids": resolved["bench_fpl_ids"],
                "captain_fpl_id": resolved["captain_fpl_id"],
                "vice_captain_fpl_id": resolved["vice_captain_fpl_id"],
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["horizon"] == [2, 3, 4]
    assert response.json()["lineups"][0]["current_setup_comparison"]["marginal_xpts"] == 10.0


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


def test_transfer_scan_can_be_disabled_by_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _use_release(tmp_path, monkeypatch)
    monkeypatch.setenv("FPL_TRANSFER_SCAN_ENABLED", "false")

    response = client.post(
        "/api/recommend/transfers",
        json={"fpl_ids": list(range(1, 16))},
    )

    assert response.status_code == 503
    assert "disabled by the operator" in response.json()["detail"]
