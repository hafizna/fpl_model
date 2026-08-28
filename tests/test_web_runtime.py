from __future__ import annotations

from pathlib import Path

import pytest

from scripts.run_web_app import runtime_address
from scripts.smoke_test_web import check_web_runtime

ROOT = Path(__file__).resolve().parents[1]


def _runtime_responses() -> dict[str, dict]:
    return {
        "/api/live": {"ok": True, "service": "fpl-web-api"},
        "/api/ready": {
            "ready": True,
            "release_id": "release_test",
            "release_health": "shadow",
            "horizon": [2, 3, 4],
            "catalog_players": 1,
            "rating_benchmark_status": "ready",
            "rating_benchmark_id": "benchmark_test",
        },
        "/api/bootstrap": {
            "release": {
                "release_id": "release_test",
                "health": "shadow",
                "model_runs": [
                    {"gameweek": 2},
                    {"gameweek": 3},
                    {"gameweek": 4},
                ],
            },
            "players": [{"fpl_id": 1}],
        },
    }


def test_runtime_address_supports_conventional_platform_port(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FPL_WEB_PORT", raising=False)
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("FPL_WEB_HOST", "0.0.0.0")

    assert runtime_address() == ("0.0.0.0", 9000)


@pytest.mark.parametrize("value", ["nope", "0", "65536"])
def test_runtime_address_rejects_invalid_ports(monkeypatch: pytest.MonkeyPatch, value: str):
    monkeypatch.setenv("FPL_WEB_PORT", value)

    with pytest.raises(ValueError, match="must be"):
        runtime_address()


def test_smoke_contract_verifies_one_consistent_release(monkeypatch: pytest.MonkeyPatch):
    responses = _runtime_responses()
    monkeypatch.setattr(
        "scripts.smoke_test_web._get_json",
        lambda base_url, path, timeout_seconds: responses[path],
    )

    report = check_web_runtime(
        "https://alpha.example/",
        expected_release_id="release_test",
    )

    assert report["passes"] is True
    assert report["release_id"] == "release_test"
    assert report["rating_benchmark_id"] == "benchmark_test"


def test_smoke_contract_rejects_cross_release_bootstrap(monkeypatch: pytest.MonkeyPatch):
    responses = _runtime_responses()
    responses["/api/bootstrap"]["release"]["release_id"] = "different_release"
    monkeypatch.setattr(
        "scripts.smoke_test_web._get_json",
        lambda base_url, path, timeout_seconds: responses[path],
    )

    with pytest.raises(ValueError, match="release IDs differ"):
        check_web_runtime("https://alpha.example")


def test_container_contract_is_non_root_and_release_aware():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "FROM python:3.13-slim" in dockerfile
    assert "USER fpl" in dockerfile
    assert "PORT=8000" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "/api/ready" in dockerfile
    assert "COPY web ./web" in dockerfile
    assert "web/release.json" not in dockerignore
    assert "data" in dockerignore.splitlines()
