from __future__ import annotations

import duckdb

from fpl_model.storage import initialize_database
from fpl_model.validation.role_state import (
    LIKELY_BENCH,
    LIKELY_STARTER,
    UNAVAILABLE,
    UNKNOWN,
    load_role_states,
    role_state_report,
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
                ('snapshot', '2026-27', 1, 1001, 'A', 'Starter', 'Starter', 1, 'MID', 8.0, 'a'),
                ('snapshot', '2026-27', 2, 1002, 'B', 'Bench', 'Bench', 1, 'FWD', 5.0, 'a'),
                ('snapshot', '2026-27', 3, 1003, 'C', 'Hurt', 'Hurt', 1, 'DEF', 6.0, 'i'),
                ('snapshot', '2026-27', 4, 1004, 'D', 'NoProj', 'NoProj', 1, 'DEF', 4.5, 'a');

            INSERT INTO model_run (
                model_run_id, target_gameweek, as_of, deadline, model_version,
                source_ingestion_run_id, status, completed_at
            ) VALUES (
                'model', 2, '2026-08-20T00:00:00+00:00', '2026-08-29T00:00:00+00:00',
                'test', 'snapshot', 'completed', current_timestamp
            );

            INSERT INTO availability_resolution_run (
                resolution_run_id, source_ingestion_run_id, target_gameweek,
                as_of, deadline, policy_version, status
            ) VALUES (
                'availability', 'snapshot', 2, '2026-08-20T00:00:00+00:00',
                '2026-08-29T00:00:00+00:00', 'test', 'completed'
            );
            INSERT INTO player_availability_resolution VALUES
                ('availability', 1, 1001, 'a', NULL, 1.0, TRUE,
                 'official_fpl_status', NULL, 'test', '[]'),
                ('availability', 2, 1002, 'a', NULL, 1.0, TRUE,
                 'official_fpl_status', NULL, 'test', '[]'),
                ('availability', 3, 1003, 'i', 0, 0.0, FALSE,
                 'official_fpl_status', NULL, 'test', '[]'),
                ('availability', 4, 1004, 'a', NULL, 1.0, TRUE,
                 'official_fpl_status', NULL, 'test', '[]');

            INSERT INTO appearance_history_import_run VALUES (
                'appearance_history', '2025-26', 'test', 'test.csv', 'sha',
                '2026-08-20T00:00:00+00:00', 1, 'completed'
            );
            INSERT INTO appearance_projection_run (
                projection_run_id, availability_resolution_run_id,
                appearance_history_import_run_id, target_gameweek,
                policy_version, status
            ) VALUES (
                'appearance', 'availability', 'appearance_history', 2,
                'test', 'completed'
            );

            INSERT INTO player_fixture_history_import_run VALUES (
                'fixture_history', '2025-26', 'vaastav', 'revision',
                '2026-06-01T00:00:00+00:00', '2026-08-18T09:00:00+00:00',
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
                'strength-sha', '2026-08-18T09:00:00+00:00', 20, 'completed'
            );
            INSERT INTO team_strength_run (
                strength_run_id, source_import_run_id, source_ingestion_run_id,
                target_gameweek, policy_version, team_rows, status
            ) VALUES (
                'strength', 'strength_import', 'snapshot', 2, 'test', 20, 'completed'
            );

            INSERT INTO baseline_projection_run (
                model_run_id, appearance_projection_run_id, player_rate_run_id,
                team_strength_run_id, policy_version, current_players,
                candidate_fixture_rows, projected_fixture_rows, gap_players, status
            ) VALUES (
                'model', 'appearance', 'rates', 'strength', 'test', 4, 3, 3, 1,
                'completed_with_gaps'
            );

            -- player 1: high start_probability -> LIKELY_STARTER.
            -- player 2: low start/appearance -> LIKELY_BENCH.
            -- player 3: unavailable (see availability resolution above).
            -- player 4: eligible but has NO player_fixture_projection row -> UNKNOWN.
            INSERT INTO player_fixture_projection VALUES
                ('model', 1001, 101, 1, 2, TRUE, 0.9, 0.05, 82.0, 5.0, 0.0, 5.0, NULL, '[]'),
                ('model', 1002, 102, 1, 2, TRUE, 0.1, 0.05, 20.0, 1.0, 0.0, 1.0, NULL, '[]'),
                ('model', 1003, 103, 1, 2, TRUE, 0.9, 0.05, 82.0, 4.0, 0.0, 4.0, NULL, '[]');
            """
        )


def test_derives_likely_starter_from_high_start_probability(tmp_path):
    database_path = tmp_path / "role_state.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        result = load_role_states(connection, model_run_id="model", fpl_ids=(1, 2, 3, 4))

    assert result[1].role_state == LIKELY_STARTER


def test_derives_likely_bench_from_low_probabilities(tmp_path):
    database_path = tmp_path / "role_state.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        result = load_role_states(connection, model_run_id="model", fpl_ids=(1, 2, 3, 4))

    assert result[2].role_state == LIKELY_BENCH


def test_resolved_ineligible_player_is_unavailable_even_with_high_projection(tmp_path):
    database_path = tmp_path / "role_state.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        result = load_role_states(connection, model_run_id="model", fpl_ids=(1, 2, 3, 4))

    # Player 3 has a high start_probability projection but is resolved
    # ineligible (e.g. a confirmed injury after the projection was made) --
    # eligibility must win.
    assert result[3].role_state == UNAVAILABLE


def test_player_with_no_projection_row_is_unknown_not_bench(tmp_path):
    database_path = tmp_path / "role_state.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        result = load_role_states(connection, model_run_id="model", fpl_ids=(1, 2, 3, 4))

    # Player 4 is eligible but has no player_fixture_projection row at all --
    # this must read as UNKNOWN (evidence missing), never silently as
    # LIKELY_BENCH (evidence present and low).
    assert result[4].role_state == UNKNOWN


def test_empty_fpl_ids_returns_empty_mapping(tmp_path):
    database_path = tmp_path / "role_state.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        result = load_role_states(connection, model_run_id="model", fpl_ids=())

    assert result == {}


def test_role_state_report_serialises_and_handles_none(tmp_path):
    database_path = tmp_path / "role_state.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        result = load_role_states(connection, model_run_id="model", fpl_ids=(1,))

    report = role_state_report(result[1])
    assert report == {"role_state": LIKELY_STARTER, "reason": result[1].reason}
    assert role_state_report(None) is None
