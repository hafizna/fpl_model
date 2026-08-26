from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fpl_model.validation import release_materialization as module


class FakeClient:
    def snapshot_payload(self):
        return {"events": []}, [], datetime(2026, 8, 26, 8, tzinfo=UTC)

    def event_live(self, gameweek: int):
        return {"elements": [], "gameweek": gameweek}


def test_materialize_inseason_release_runs_complete_stage_order(monkeypatch, tmp_path: Path):
    calls: list[str] = []

    def stage(name: str, result: SimpleNamespace):
        def run(*args, **kwargs):
            calls.append(name)
            return result

        return run

    monkeypatch.setattr(
        module,
        "persist_fpl_snapshot",
        stage(
            "snapshot",
            SimpleNamespace(
                ingestion_run_id="snapshot_final",
                captured_at=datetime(2026, 8, 26, 8, tzinfo=UTC),
                players=600,
                fixtures=380,
            ),
        ),
    )
    monkeypatch.setattr(
        module,
        "import_player_identity_bridge",
        stage(
            "identity",
            SimpleNamespace(
                bridge_run_id="bridge_final",
                status="completed_with_gaps",
                matched_players=500,
                official_only_players=100,
                vaastav_only_players=200,
                name_mismatch_players=0,
            ),
        ),
    )
    monkeypatch.setattr(
        module,
        "materialize_latest_fpl_availability",
        stage(
            "availability",
            SimpleNamespace(
                source_ingestion_run_id="snapshot_final",
                resolution_run_id="availability_final",
                status="completed",
            ),
        ),
    )
    monkeypatch.setattr(
        module,
        "persist_fpl_event_live",
        stage(
            "event_live",
            SimpleNamespace(gameweek=1, live_run_id="live_final", status="completed"),
        ),
    )
    monkeypatch.setattr(
        module,
        "materialize_inseason_appearance",
        stage(
            "appearance",
            SimpleNamespace(projection_run_id="appearance_final", status="completed"),
        ),
    )
    monkeypatch.setattr(
        module,
        "import_team_strength_history",
        stage("strength_import", SimpleNamespace(import_run_id="strength_import_final")),
    )
    monkeypatch.setattr(
        module,
        "materialize_preseason_team_strength",
        stage("strength", SimpleNamespace(strength_run_id="strength_final")),
    )
    monkeypatch.setattr(
        module,
        "materialize_context_features",
        stage(
            "context",
            SimpleNamespace(context_run_id="context_final", status="completed"),
        ),
    )
    monkeypatch.setattr(
        module,
        "materialize_inseason_baseline",
        stage("anchor", SimpleNamespace(model_run_id="run_gw2", status="completed")),
    )
    monkeypatch.setattr(
        module,
        "materialize_frozen_projection_horizon",
        stage(
            "horizon",
            SimpleNamespace(
                start_gameweek=2,
                end_gameweek=4,
                model_run_ids=("run_gw2", "run_gw3", "run_gw4"),
            ),
        ),
    )
    monkeypatch.setattr(
        module,
        "materialize_shadow_calibration",
        stage(
            "calibration",
            SimpleNamespace(model_run_id="run", artifact_id="cal", player_fixture_rows=1),
        ),
    )
    monkeypatch.setattr(
        module,
        "apply_uncertainty_artifact",
        stage(
            "uncertainty",
            SimpleNamespace(
                model_run_id="run",
                artifact_id="unc",
                player_fixture_rows=1,
                application_mode="shadow",
            ),
        ),
    )
    monkeypatch.setattr(
        module,
        "orchestrate_release_validation",
        stage("validate", SimpleNamespace(report={"passes": True})),
    )
    monkeypatch.setattr(
        module,
        "determine_release_health",
        stage("health", SimpleNamespace(report={"state": "shadow"})),
    )

    result = module.materialize_inseason_release(
        target_gameweek=2,
        current_season="2026-27",
        previous_season="2025-26",
        team_strength_csv=tmp_path / "strength.csv",
        team_strength_source_label="reviewed",
        vaastav_players_csv=tmp_path / "players_raw.csv",
        vaastav_source_revision="abc123",
        calibration_artifact_id="cal",
        uncertainty_artifact_id="unc",
        database_path=tmp_path / "model.duckdb",
        client=FakeClient(),
    )

    assert result.model_run_ids == ("run_gw2", "run_gw3", "run_gw4")
    assert result.health == "shadow"
    assert calls == [
        "snapshot",
        "identity",
        "availability",
        "event_live",
        "appearance",
        "strength_import",
        "strength",
        "context",
        "anchor",
        "horizon",
        "calibration",
        "uncertainty",
        "calibration",
        "uncertainty",
        "calibration",
        "uncertainty",
        "validate",
        "health",
    ]
