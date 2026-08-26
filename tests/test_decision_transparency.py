from __future__ import annotations

import duckdb

from fpl_model.storage import initialize_database
from fpl_model.validation.decision_transparency import (
    load_player_transparency,
    transparency_report,
)


def _seed(database_path) -> None:
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('snapshot', 'official_fpl_api', '2026-08-20T00:00:00+00:00', 'completed');

            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES
                ('snapshot', '2026-27', 1, 1001, 'A', 'One', 'One', 1, 'MID', 8.0, 'a'),
                ('snapshot', '2026-27', 2, 1002, 'B', 'Two', 'Two', 1, 'FWD', 9.0, 'a');

            INSERT INTO model_run (
                model_run_id, target_gameweek, as_of, deadline, model_version,
                source_ingestion_run_id, status, completed_at
            ) VALUES (
                'model', 2, '2026-08-20T00:00:00+00:00', '2026-08-29T00:00:00+00:00',
                'test', 'snapshot', 'completed', current_timestamp
            );

            -- player 1 (code 1001) has a double gameweek: two fixtures.
            INSERT INTO player_fixture_projection VALUES
                ('model', 1001, 101, 1, 2, TRUE, 0.9, 0.05, 82.0, 5.0, 0.0, 5.0, NULL, '[]'),
                ('model', 1001, 102, 1, 3, FALSE, 0.9, 0.05, 82.0, 4.0, 0.0, 4.0, NULL, '[]'),
                ('model', 1002, 103, 2, 3, TRUE, 0.8, 0.05, 75.0, 6.0, 0.0, 6.0, NULL, '[]');

            INSERT INTO shadow_calibration_artifact (
                artifact_id, calibration_type, source_season, source_model_version,
                source_reference, training_rows, training_gameweeks, slope, intercept,
                policy_version, status
            ) VALUES (
                'calib_1', 'xpts', '2025-26', 'test', 'ref.json', 100, 10, 0.8, 0.2, 'test', 'shadow'
            );
            INSERT INTO player_fixture_shadow_projection VALUES
                ('model', 1001, 101, 'calib_1', 5.0, 4.2, FALSE),
                ('model', 1001, 102, 'calib_1', 4.0, 3.4, FALSE);

            INSERT INTO uncertainty_artifact (
                artifact_id, source_season, source_model_version, source_reference,
                policy_version, interval_mass, minimum_segment_rows,
                minimum_segment_gameweeks, low_risk_rmse_threshold,
                high_risk_rmse_threshold, segment_rows, status
            ) VALUES (
                'unc_1', '2025-26', 'test', 'ref.json', 'test', 0.8, 100, 5, 2.4, 3.1, 100, 'shadow'
            );
            INSERT INTO player_fixture_uncertainty VALUES
                ('model', 1001, 101, 'unc_1', 2.0, 8.0, 2.0, 0.4, 'medium', 'position', 100, '[]'),
                ('model', 1001, 102, 'unc_1', 1.0, 7.0, 1.5, 0.375, 'low', 'position', 100, '[]');
            """
        )


def test_aggregates_double_gameweek_calibration_and_uncertainty_across_fixtures(tmp_path):
    database_path = tmp_path / "transparency.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        result = load_player_transparency(
            connection, model_run_id="model", fpl_ids=(1, 2)
        )

    player1 = result[1]
    assert player1.raw_xpts == 9.0  # 5.0 + 4.0 across two fixtures
    assert player1.shadow_calibrated_xpts == 7.6  # 4.2 + 3.4
    assert player1.calibration_artifact_id == "calib_1"
    assert player1.calibration_status == "shadow"
    assert player1.lower_xpts == 3.0  # 2.0 + 1.0
    assert player1.upper_xpts == 15.0  # 8.0 + 7.0
    assert player1.predictive_rmse == (2.0**2 + 1.5**2) ** 0.5
    assert player1.risk_band == "medium"  # worst of medium/low
    assert player1.uncertainty_artifact_id == "unc_1"
    assert player1.uncertainty_status == "shadow"


def test_player_without_shadow_data_reports_none_but_does_not_raise(tmp_path):
    database_path = tmp_path / "transparency.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        result = load_player_transparency(
            connection, model_run_id="model", fpl_ids=(1, 2)
        )

    player2 = result[2]
    assert player2.raw_xpts == 0.0
    assert player2.shadow_calibrated_xpts is None
    assert player2.calibration_artifact_id is None
    assert player2.lower_xpts is None
    assert player2.upper_xpts is None
    assert player2.predictive_rmse is None
    assert player2.risk_band is None


def test_empty_fpl_ids_returns_empty_mapping(tmp_path):
    database_path = tmp_path / "transparency.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        result = load_player_transparency(connection, model_run_id="model", fpl_ids=())

    assert result == {}


def test_transparency_report_serialises_and_handles_none():
    with_data = None
    assert transparency_report(with_data) is None


def test_transparency_report_renders_all_fields(tmp_path):
    database_path = tmp_path / "transparency.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        result = load_player_transparency(connection, model_run_id="model", fpl_ids=(1,))

    report = transparency_report(result[1])
    assert report == {
        "raw_xpts": 9.0,
        "shadow_calibrated_xpts": 7.6,
        "calibration_artifact_id": "calib_1",
        "calibration_status": "shadow",
        "lower_xpts": 3.0,
        "upper_xpts": 15.0,
        "predictive_rmse": (2.0**2 + 1.5**2) ** 0.5,
        "risk_band": "medium",
        "uncertainty_artifact_id": "unc_1",
        "uncertainty_status": "shadow",
    }
