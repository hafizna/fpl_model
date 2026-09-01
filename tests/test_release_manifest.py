from __future__ import annotations

import duckdb
import pytest

from fpl_model.storage import initialize_database
from fpl_model.validation.release_manifest import build_release_manifest


def _seed_horizon(database_path) -> None:
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('snapshot', 'fpl_api', '2026-08-18T09:00:00+07:00', 'completed');

            INSERT INTO player_identity_bridge_run (
                bridge_run_id, source_ingestion_run_id, target_season,
                vaastav_season, source_revision, source_path, source_sha256,
                policy_version, official_players, vaastav_players,
                matched_players, official_only_players, vaastav_only_players,
                name_mismatch_players, status
            ) VALUES (
                'bridge', 'snapshot', '2026-27', '2025-26', 'rev', 'players.csv',
                'sha', 'test', 5, 5, 5, 0, 0, 0, 'completed'
            );

            INSERT INTO availability_resolution_run (
                resolution_run_id, source_ingestion_run_id, target_gameweek,
                as_of, deadline, policy_version, status
            ) VALUES (
                'availability', 'snapshot', 1, '2026-08-18T09:00:00+07:00',
                '2026-08-22T00:30:00+07:00', 'test', 'completed'
            );

            INSERT INTO appearance_history_import_run VALUES (
                'appearance_history', '2025-26', 'test', 'test.csv', 'sha',
                '2026-08-18T09:00:00+07:00', 1, 'completed'
            );

            INSERT INTO appearance_projection_run (
                projection_run_id, availability_resolution_run_id,
                appearance_history_import_run_id, target_gameweek,
                policy_version, status
            ) VALUES (
                'appearance', 'availability', 'appearance_history', 1,
                'test', 'completed'
            );

            INSERT INTO player_fixture_history_import_run VALUES (
                'fixture_history', '2025-26', 'vaastav', 'revision',
                '2026-06-01T00:00:00+00:00', '2026-08-18T09:00:00+07:00',
                'players.csv', 'gws.csv', 'players-sha', 'gws-sha', 1, 1, 0,
                'completed'
            );
            INSERT INTO player_rate_history_run (
                rate_run_id, source_import_run_id, long_form_gameweeks,
                short_form_gameweeks, defcon_short_form_gameweeks,
                policy_version, player_rows, status
            ) VALUES ('rates', 'fixture_history', 38, 6, 10, 'test', 1, 'completed');

            INSERT INTO team_strength_import_run VALUES (
                'strength_import', '2026-27', '2025-26', 'test', 'strength.csv',
                'strength-sha', '2026-08-18T09:00:00+07:00', 20, 'completed'
            );
            INSERT INTO team_strength_run (
                strength_run_id, source_import_run_id, source_ingestion_run_id,
                target_gameweek, policy_version, team_rows, status
            ) VALUES (
                'strength', 'strength_import', 'snapshot', 1, 'test', 20, 'completed'
            );

            INSERT INTO model_run (
                model_run_id, target_gameweek, as_of, deadline, model_version,
                source_ingestion_run_id, status
            ) VALUES (
                'baseline_gw1', 1, '2026-08-18T09:00:00+07:00',
                '2026-08-22T00:30:00+07:00', 'test_v1', 'snapshot', 'completed'
            );
            INSERT INTO baseline_projection_run (
                model_run_id, appearance_projection_run_id, player_rate_run_id,
                team_strength_run_id, policy_version, current_players,
                candidate_fixture_rows, projected_fixture_rows, gap_players, status
            ) VALUES (
                'baseline_gw1', 'appearance', 'rates', 'strength', 'test', 5, 5, 5, 0,
                'completed'
            );

            INSERT INTO uncertainty_artifact (
                artifact_id, source_season, source_model_version, source_reference,
                policy_version, interval_mass, minimum_segment_rows,
                minimum_segment_gameweeks, low_risk_rmse_threshold,
                high_risk_rmse_threshold, segment_rows, status
            ) VALUES (
                'uncertainty_1', '2025-26', 'test_v1', 'ref.json', 'test', 0.8, 100, 5,
                2.4, 3.1, 100, 'shadow'
            );
            INSERT INTO model_uncertainty_lineage (
                model_run_id, artifact_id, application_mode
            ) VALUES ('baseline_gw1', 'uncertainty_1', 'shadow');

            INSERT INTO shadow_calibration_artifact (
                artifact_id, calibration_type, source_season, source_model_version,
                source_reference, training_rows, training_gameweeks, slope,
                intercept, policy_version, status
            ) VALUES (
                'calibration_1', 'xpts', '2025-26', 'test_v1', 'ref.json', 100, 5,
                0.9, 0.1, 'test', 'shadow'
            );
            INSERT INTO model_shadow_calibration_lineage (
                model_run_id, artifact_id
            ) VALUES ('baseline_gw1', 'calibration_1');
            """
        )


def test_build_release_manifest_links_full_lineage_and_passes(tmp_path):
    database_path = tmp_path / "release.duckdb"
    _seed_horizon(database_path)

    manifest = build_release_manifest(
        model_run_ids=("baseline_gw1",), database_path=database_path
    )

    assert manifest.passes_linkage_gate is True
    assert manifest.report["linkage"]["problems"] == []
    gw1 = manifest.report["gameweeks"][0]
    assert gw1["model_run"]["model_run_id"] == "baseline_gw1"
    assert gw1["player_identity_bridge_run"]["bridge_run_id"] == "bridge"
    assert gw1["appearance_lineage"]["kind"] == "preseason"
    assert gw1["player_rate_run"]["rate_run_id"] == "rates"
    assert gw1["team_strength_run"]["strength_run_id"] == "strength"
    assert gw1["shadow_calibration"]["artifact_id"] == "calibration_1"
    assert gw1["uncertainty"]["artifact_id"] == "uncertainty_1"
    assert gw1["context_run"] is None
    assert manifest.report["shadow_status"]["context_missing_for_gameweeks"] == [1]
    assert manifest.manifest_id.startswith("release_manifest_")


def test_build_release_manifest_is_deterministic_given_the_same_runs(tmp_path):
    database_path = tmp_path / "release.duckdb"
    _seed_horizon(database_path)

    first = build_release_manifest(model_run_ids=("baseline_gw1",), database_path=database_path)
    second = build_release_manifest(model_run_ids=("baseline_gw1",), database_path=database_path)

    assert first.manifest_id == second.manifest_id
    assert first.report == second.report


def test_build_release_manifest_rejects_empty_input(tmp_path):
    database_path = tmp_path / "release.duckdb"
    initialize_database(database_path)

    with pytest.raises(ValueError, match="at least one model_run_id"):
        build_release_manifest(model_run_ids=(), database_path=database_path)


def test_build_release_manifest_rejects_duplicate_run_ids(tmp_path):
    database_path = tmp_path / "release.duckdb"
    _seed_horizon(database_path)

    with pytest.raises(ValueError, match="duplicates"):
        build_release_manifest(
            model_run_ids=("baseline_gw1", "baseline_gw1"), database_path=database_path
        )


def test_build_release_manifest_fails_closed_on_mismatched_snapshots(tmp_path):
    database_path = tmp_path / "release.duckdb"
    _seed_horizon(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('snapshot2', 'fpl_api', '2026-08-25T09:00:00+07:00', 'completed');
            INSERT INTO model_run (
                model_run_id, target_gameweek, as_of, deadline, model_version,
                source_ingestion_run_id, status
            ) VALUES (
                'baseline_gw2', 2, '2026-08-25T09:00:00+07:00',
                '2026-08-29T00:30:00+07:00', 'test_v1', 'snapshot2', 'completed'
            );
            """
        )

    manifest = build_release_manifest(
        model_run_ids=("baseline_gw1", "baseline_gw2"), database_path=database_path
    )

    assert manifest.passes_linkage_gate is False
    problems = "\n".join(manifest.report["linkage"]["problems"])
    assert "do not share one official snapshot" in problems
    assert "do not share one frozen as_of" in problems
    assert "GW2: no baseline_projection_run linked" in problems


def test_build_release_manifest_rejects_out_of_order_gameweeks(tmp_path):
    database_path = tmp_path / "release.duckdb"
    _seed_horizon(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO model_run (
                model_run_id, target_gameweek, as_of, deadline, model_version,
                source_ingestion_run_id, status
            ) VALUES (
                'baseline_gw0', 0, '2026-08-18T09:00:00+07:00',
                '2026-08-20T00:30:00+07:00', 'test_v1', 'snapshot', 'completed'
            );
            """
        )

    manifest = build_release_manifest(
        model_run_ids=("baseline_gw1", "baseline_gw0"), database_path=database_path
    )

    assert manifest.passes_linkage_gate is False
    assert any(
        "ordered by ascending target_gameweek" in problem
        for problem in manifest.report["linkage"]["problems"]
    )


def test_build_release_manifest_raises_on_unknown_run_id(tmp_path):
    database_path = tmp_path / "release.duckdb"
    initialize_database(database_path)

    with pytest.raises(ValueError, match="unknown model_run_id"):
        build_release_manifest(model_run_ids=("nope",), database_path=database_path)


def test_build_release_manifest_surfaces_non_final_event_live_evidence(tmp_path):
    database_path = tmp_path / "release.duckdb"
    _seed_horizon(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO fpl_event_live_run (
                live_run_id, source_ingestion_run_id, season, gameweek, captured_at,
                source_path, source_sha256, event_finished, data_checked,
                player_rows, status
            ) VALUES (
                'live_gw1', 'snapshot', '2026-27', 1, '2026-08-25T09:00:00+07:00',
                'live.json', 'sha', TRUE, FALSE, 5, 'provisional'
            );

            INSERT INTO availability_resolution_run (
                resolution_run_id, source_ingestion_run_id, target_gameweek,
                as_of, deadline, policy_version, status
            ) VALUES (
                'availability2', 'snapshot', 2, '2026-08-18T09:00:00+07:00',
                '2026-08-29T00:30:00+07:00', 'test', 'completed'
            );
            INSERT INTO appearance_projection_run (
                projection_run_id, availability_resolution_run_id,
                appearance_history_import_run_id, target_gameweek,
                policy_version, status
            ) VALUES (
                'appearance_inseason', 'availability2', 'appearance_history', 2,
                'test', 'completed'
            );
            INSERT INTO inseason_appearance_run (
                projection_run_id, current_season, previous_season,
                first_history_gameweek, last_history_gameweek, live_run_ids,
                previous_effective_fixtures, as_of, policy_version
            ) VALUES (
                'appearance_inseason', '2026-27', '2025-26', 1, 1,
                '["live_gw1"]', 5.0, '2026-08-18T09:00:00+07:00', 'test'
            );

            INSERT INTO model_run (
                model_run_id, target_gameweek, as_of, deadline, model_version,
                source_ingestion_run_id, status
            ) VALUES (
                'baseline_gw2', 2, '2026-08-18T09:00:00+07:00',
                '2026-08-29T00:30:00+07:00', 'test_v1', 'snapshot', 'completed'
            );
            INSERT INTO baseline_projection_run (
                model_run_id, appearance_projection_run_id, player_rate_run_id,
                team_strength_run_id, policy_version, current_players,
                candidate_fixture_rows, projected_fixture_rows, gap_players, status
            ) VALUES (
                'baseline_gw2', 'appearance_inseason', 'rates', 'strength', 'test',
                5, 5, 5, 0, 'completed'
            );
            """
        )

    manifest = build_release_manifest(
        model_run_ids=("baseline_gw1", "baseline_gw2"), database_path=database_path
    )

    assert manifest.passes_linkage_gate is True
    gw2 = manifest.report["gameweeks"][1]
    assert gw2["appearance_lineage"]["kind"] == "inseason"
    assert gw2["appearance_lineage"]["event_live_runs"] == [
        {
            "live_run_id": "live_gw1",
            "season": "2026-27",
            "gameweek": 1,
            "captured_at": "2026-08-25T02:00:00+00:00",
            "event_finished": True,
            "data_checked": False,
            "player_rows": 5,
            "status": "provisional",
        }
    ]
    non_final = manifest.report["shadow_status"]["non_final_event_live_runs"]
    assert non_final == [
        {
            "target_gameweek": 2,
            "live_run_id": "live_gw1",
            "event_gameweek": 1,
            "event_finished": True,
            "data_checked": False,
        }
    ]
