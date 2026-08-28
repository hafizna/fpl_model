from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fpl_model.operations.deadline_refresh import DeadlineRefreshConfig, run_deadline_refresh
from fpl_model.validation.release_materialization import ReleaseMaterializationResult
from fpl_model.webapp.release_export import WebReleaseExport

STARTED = datetime(2026, 8, 28, 8, 0, tzinfo=UTC)


class _Response:
    status_code = 204

    def raise_for_status(self) -> None:
        return None


def _config(tmp_path: Path, *, webhook: str | None = "https://alerts.example.test/hook"):
    database = tmp_path / "fpl.duckdb"
    database.write_bytes(b"known-good-database")
    release = tmp_path / "release.json"
    release.write_text(
        json.dumps({"release": {"release_id": "web_release_previous"}}),
        encoding="utf-8",
    )
    return DeadlineRefreshConfig(
        target_gameweek=2,
        current_season="2026-27",
        previous_season="2025-26",
        team_strength_csv=tmp_path / "team-strength.csv",
        team_strength_source_label="reviewed source",
        vaastav_players_csv=tmp_path / "players.csv",
        vaastav_source_revision="pinned-revision",
        calibration_artifact_id="calibration_1",
        uncertainty_artifact_id="uncertainty_1",
        database_path=database,
        web_release_output=release,
        status_output=tmp_path / "status.json",
        materialization_report_output=tmp_path / "materialization.json",
        lock_file=tmp_path / "refresh.lock",
        backup_directory=tmp_path / "backups",
        alert_webhook_url=webhook,
    )


def _materialized(*, passes: bool = True) -> ReleaseMaterializationResult:
    return ReleaseMaterializationResult(
        report={
            "release": {"model_run_ids": ["run_gw2", "run_gw3", "run_gw4"]},
            "validation": {"passes": passes, "health": {"state": "shadow"}},
        }
    )


def _exported() -> WebReleaseExport:
    return WebReleaseExport(
        payload={
            "schema_version": "fpl_web_release_v1",
            "release": {"release_id": "web_release_new", "health": "shadow"},
            "players": [],
        }
    )


def _clock():
    values = iter((STARTED, STARTED + timedelta(seconds=12)))
    return lambda: next(values)


def test_success_backs_up_database_and_atomically_publishes_release(tmp_path: Path):
    config = _config(tmp_path)
    materialize_calls = []
    export_calls = []
    alert_calls = []

    def materialize(**kwargs):
        materialize_calls.append(kwargs)
        return _materialized()

    def export(**kwargs):
        export_calls.append(kwargs)
        return _exported()

    def post(url, **kwargs):
        alert_calls.append((url, kwargs))
        return _Response()

    result = run_deadline_refresh(
        config,
        materialize=materialize,
        export=export,
        post_alert=post,
        clock=_clock(),
    )

    assert result.succeeded
    assert result.exit_code == 0
    assert result.report["previous_release_id"] == "web_release_previous"
    assert result.report["release_id"] == "web_release_new"
    assert result.report["alert"] == {"status": "delivered", "http_status": 204}
    assert json.loads(config.web_release_output.read_text(encoding="utf-8"))["release"][
        "release_id"
    ] == "web_release_new"
    assert json.loads(config.status_output.read_text(encoding="utf-8"))["status"] == "succeeded"
    assert json.loads(config.materialization_report_output.read_text(encoding="utf-8"))[
        "validation"
    ]["passes"] is True
    backups = list(config.backup_directory.glob("*.duckdb"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"known-good-database"
    assert not config.lock_file.exists()
    assert materialize_calls[0]["target_gameweek"] == 2
    assert export_calls == [
        {
            "model_run_ids": ("run_gw2", "run_gw3", "run_gw4"),
            "require_production": False,
            "database_path": config.database_path,
        }
    ]
    assert alert_calls[0][0] == config.alert_webhook_url
    assert alert_calls[0][1]["json"]["status"] == "succeeded"
    assert alert_calls[0][1]["json"]["event"] == "fpl_deadline_refresh"


def test_materialization_failure_preserves_previous_release_and_alerts(tmp_path: Path):
    config = _config(tmp_path)
    previous_bytes = config.web_release_output.read_bytes()
    alerts = []

    def fail_materialization(**kwargs):
        raise ValueError("official Gameweek is not final")

    def should_not_export(**kwargs):
        raise AssertionError("export must not run")

    def post(url, **kwargs):
        alerts.append(kwargs["json"])
        return _Response()

    result = run_deadline_refresh(
        config,
        materialize=fail_materialization,
        export=should_not_export,
        post_alert=post,
        clock=_clock(),
    )

    assert not result.succeeded
    assert result.exit_code == 2
    assert result.report["stage"] == "materialize"
    assert result.report["error"]["message"] == "official Gameweek is not final"
    assert config.web_release_output.read_bytes() == previous_bytes
    assert alerts[0]["status"] == "failed"
    assert not config.lock_file.exists()


def test_failed_validation_never_reaches_export_or_publish(tmp_path: Path):
    config = _config(tmp_path)
    previous_bytes = config.web_release_output.read_bytes()

    result = run_deadline_refresh(
        config,
        materialize=lambda **kwargs: _materialized(passes=False),
        export=lambda **kwargs: pytest.fail("export must not run"),
        post_alert=lambda *args, **kwargs: _Response(),
        clock=_clock(),
    )

    assert result.report["status"] == "failed"
    assert result.report["stage"] == "materialize"
    assert config.web_release_output.read_bytes() == previous_bytes


def test_export_failure_preserves_previous_release(tmp_path: Path):
    config = _config(tmp_path)
    previous_bytes = config.web_release_output.read_bytes()

    def fail_export(**kwargs):
        raise ValueError("compact release freshness failed")

    result = run_deadline_refresh(
        config,
        materialize=lambda **kwargs: _materialized(),
        export=fail_export,
        post_alert=lambda *args, **kwargs: _Response(),
        clock=_clock(),
    )

    assert result.report["stage"] == "export"
    assert config.web_release_output.read_bytes() == previous_bytes


def test_alert_failure_is_distinct_from_a_release_failure(tmp_path: Path):
    config = _config(tmp_path)

    result = run_deadline_refresh(
        config,
        materialize=lambda **kwargs: _materialized(),
        export=lambda **kwargs: _exported(),
        post_alert=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("webhook down")),
        clock=_clock(),
    )

    assert result.succeeded
    assert result.exit_code == 3
    assert result.report["alert"]["status"] == "failed"
    assert "alerts.example.test" not in json.dumps(result.report)
    assert "alerts.example.test" not in repr(config)
    assert result.report["release_id"] == "web_release_new"


def test_existing_lock_refuses_a_second_worker_without_overwriting_status(tmp_path: Path):
    config = _config(tmp_path)
    config.lock_file.write_text("active", encoding="utf-8")

    with pytest.raises(RuntimeError, match="refresh lock already exists"):
        run_deadline_refresh(
            config,
            materialize=lambda **kwargs: pytest.fail("materialize must not run"),
            export=lambda **kwargs: pytest.fail("export must not run"),
            clock=lambda: STARTED,
        )

    assert config.lock_file.read_text(encoding="utf-8") == "active"
    assert not config.status_output.exists()


def test_missing_webhook_is_an_explicit_non_failure_state(tmp_path: Path):
    config = _config(tmp_path, webhook=None)

    result = run_deadline_refresh(
        config,
        materialize=lambda **kwargs: _materialized(),
        export=lambda **kwargs: _exported(),
        clock=_clock(),
    )

    assert result.exit_code == 0
    assert result.report["alert"] == {"status": "not_configured"}
