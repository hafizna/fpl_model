from __future__ import annotations

import json

import duckdb
import pytest

from fpl_model.model.baseline_pipeline import materialize_preseason_baseline
from fpl_model.storage import initialize_database


def _seed_baseline_inputs(database_path) -> None:
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('snapshot', 'fpl_api', '2026-08-18T09:00:00+07:00', 'completed');

            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES
                ('snapshot', '2026-27', 1, 519634, 'Jenson', 'Seelt', 'Seelt',
                 1, 'DEF', 4.0, 'a'),
                ('snapshot', '2026-27', 2, 999999, 'New', 'Player', 'New Player',
                 1, 'MID', 5.0, 'a');

            INSERT INTO team_snapshot (
                ingestion_run_id, team_id, team_code, name, short_name, unavailable
            ) VALUES
                ('snapshot', 1, 101, 'Sunderland', 'SUN', false),
                ('snapshot', 2, 102, 'Opponent', 'OPP', false);

            INSERT INTO fixture_snapshot VALUES (
                'snapshot', 100, 1, '2026-08-22T15:00:00+01:00',
                1, 2, false, false
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
            INSERT INTO player_appearance_history VALUES (
                'appearance_history', 519634, 'Jenson Seelt', 1, 1, 1, 52.0, 82.0
            );

            INSERT INTO appearance_projection_run (
                projection_run_id, availability_resolution_run_id,
                appearance_history_import_run_id, target_gameweek,
                policy_version, status
            ) VALUES (
                'appearance', 'availability', 'appearance_history', 1,
                'test', 'completed'
            );
            INSERT INTO player_appearance_projection VALUES
                ('appearance', 1, 519634, 1.0, 0.5, 0.25, 0.75, 0.3,
                 46.5, 0.75, 0.3, 1.05, '[]'),
                ('appearance', 2, 999999, 1.0, 0.5, 0.25, 0.75, 0.3,
                 35.0, 0.75, 0.3, 1.05, '[]');

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
            INSERT INTO player_rate_history VALUES (
                'rates', 519634, 'Jenson Seelt', 'DEF', 133, 1, 0, 0, 0, 0, 10,
                133, 0.05, 0.02, 133, 0.05, 0.02,
                133, 10, 133, 10, '[]'
            );

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
            INSERT INTO team_strength_projection VALUES
                ('strength', 1, 101, 'SUN', 'Sunderland', false,
                 1.4, 1.5, 1.42, 1.3, 1.4, 1.32, 1.28, 1.5, 1.5,
                 0.95, 0.9, 1.1, 1.05, '[]'),
                ('strength', 2, 102, 'OPP', 'Opponent', true,
                 1.2, 1.3, 1.22, 1.5, 1.6, 1.52, 1.48, 1.5, 1.5,
                 0.81, 1.1, 0.95, 0.98, '[]');
            """
        )


def test_materialize_preseason_baseline_records_lineage_components_and_gaps(
    tmp_path,
):
    database_path = tmp_path / "baseline.duckdb"
    _seed_baseline_inputs(database_path)

    result = materialize_preseason_baseline(
        appearance_projection_run_id="appearance",
        player_rate_run_id="rates",
        team_strength_run_id="strength",
        database_path=database_path,
    )
    repeated = materialize_preseason_baseline(
        appearance_projection_run_id="appearance",
        player_rate_run_id="rates",
        team_strength_run_id="strength",
        database_path=database_path,
    )

    assert repeated == result
    assert result.current_players == 2
    assert result.candidate_fixture_rows == 2
    assert result.projected_fixture_rows == 1
    assert result.gap_players == 1
    assert result.status == "completed_with_gaps"

    with duckdb.connect(str(database_path), read_only=True) as connection:
        projection = connection.execute(
            """
            SELECT expected_minutes, baseline_xpts, data_quality_flags
            FROM player_fixture_projection WHERE model_run_id = ?
            """,
            [result.model_run_id],
        ).fetchone()
        component_total, component_count = connection.execute(
            """
            SELECT sum(expected_points), count(*) FROM projection_component
            WHERE model_run_id = ?
            """,
            [result.model_run_id],
        ).fetchone()
        gap = connection.execute(
            """
            SELECT fpl_id, data_quality_flags FROM baseline_projection_gap
            WHERE model_run_id = ?
            """,
            [result.model_run_id],
        ).fetchone()

    assert projection[0] == pytest.approx(46.5)
    assert projection[1] == pytest.approx(component_total * 1.05)
    assert component_count == 11
    assert {
        "CAMEO_MINUTES_EXCEED_START_MINUTES",
        "OPPONENT_PROMOTED_PRIOR",
        "SPARSE_APPEARANCE_HISTORY",
    }.issubset(json.loads(projection[2]))
    assert gap[0] == 2
    assert set(json.loads(gap[1])) == {
        "MISSING_MINUTES_SCENARIO",
        "NO_PREVIOUS_PL_PLAYER_RATE_HISTORY",
    }


def test_preseason_baseline_rejects_in_season_gameweeks(tmp_path):
    with pytest.raises(ValueError, match="supports GW1 only"):
        materialize_preseason_baseline(
            target_gameweek=2,
            database_path=tmp_path / "baseline.duckdb",
        )
