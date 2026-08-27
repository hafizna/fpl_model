from __future__ import annotations

import duckdb
import pytest

from fpl_model.storage import initialize_database
from fpl_model.validation.appearance_observation import (
    NO_TEAM_FIXTURE,
    NOT_YET_ELIGIBLE,
    STARTER,
    SUBSTITUTE,
    UNAVAILABLE,
    UNUSED_SUBSTITUTE_OR_NOT_IN_SQUAD,
    load_appearance_observations,
)


def _seed(database_path, *, event_finished=True, data_checked=True) -> None:
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('snapshot', 'official_fpl_api', '2026-08-20T00:00:00+00:00', 'completed');

            -- fpl_id 1: started -> STARTER.
            -- fpl_id 2: came on as sub -> SUBSTITUTE.
            -- fpl_id 3: 0 minutes, resolved ineligible -> UNAVAILABLE.
            -- fpl_id 4: 0 minutes, no resolved block, team 1 has a fixture -> ambiguous bucket.
            -- fpl_id 5: 0 minutes, team 5 has no fixture this Gameweek -> NO_TEAM_FIXTURE.
            -- fpl_id 6: 0 minutes, not present in player_snapshot at all -> NOT_YET_ELIGIBLE.
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES
                ('snapshot', '2026-27', 1, 1001, 'A', 'Starter', 'Starter', 1, 'MID', 8.0, 'a'),
                ('snapshot', '2026-27', 2, 1002, 'B', 'Sub', 'Sub', 1, 'FWD', 6.0, 'a'),
                ('snapshot', '2026-27', 3, 1003, 'C', 'Injured', 'Injured', 1, 'DEF', 5.0, 'i'),
                ('snapshot', '2026-27', 4, 1004, 'D', 'Unused', 'Unused', 1, 'DEF', 4.5, 'a'),
                ('snapshot', '2026-27', 5, 1005, 'E', 'Blank', 'Blank', 5, 'FWD', 5.5, 'a');

            INSERT INTO fixture_snapshot (
                ingestion_run_id, fixture_id, gameweek, kickoff_time,
                home_team_id, away_team_id, started, finished
            ) VALUES ('snapshot', 501, 2, '2026-08-30T14:00:00+00:00', 1, 2, TRUE, TRUE);
            """
        )
        connection.execute(
            """
            INSERT INTO fpl_event_live_run VALUES (
                'live', 'snapshot', '2026-27', 2, '2026-08-30T18:00:00+00:00',
                'event.json', 'sha', ?, ?, 6, ?, current_timestamp
            )
            """,
            [
                event_finished,
                data_checked,
                "completed" if (event_finished and data_checked) else "provisional",
            ],
        )
        connection.execute(
            """
            INSERT INTO player_gameweek_stat VALUES
                ('live', 1, 1001, TRUE, 90, 1, 1, 0, 0, 0, 0, 5, 40, 0, 0.2, 0.1, 0.4, 6, FALSE, '[]'),
                ('live', 2, 1002, TRUE, 15, 0, 0, 0, 0, 0, 0, 0, 5, 0, 0.0, 0.0, 0.0, 1, FALSE, '[]'),
                ('live', 3, 1003, FALSE, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0, FALSE, '[]'),
                ('live', 4, 1004, FALSE, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0, FALSE, '[]'),
                ('live', 5, 1005, FALSE, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0, FALSE, '[]'),
                ('live', 6, NULL, FALSE, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0, FALSE, '[]')
            """
        )
        connection.execute(
            """
            INSERT INTO availability_resolution_run (
                resolution_run_id, source_ingestion_run_id, target_gameweek,
                as_of, deadline, policy_version, status
            ) VALUES (
                'resolution', 'snapshot', 2, '2026-08-29T00:00:00+00:00',
                '2026-08-30T11:00:00+00:00', 'test', 'completed'
            );

            INSERT INTO player_availability_resolution VALUES
                ('resolution', 1, 1001, 'a', NULL, 1.0, TRUE, 'official_fpl_status', NULL, 'test', '[]'),
                ('resolution', 3, 1003, 'i', 0, 0.0, FALSE, 'official_fpl_status', NULL, 'test', '[]'),
                ('resolution', 4, 1004, 'a', NULL, 1.0, TRUE, 'official_fpl_status', NULL, 'test', '[]')
            """
        )


def test_classifies_every_named_observation_shape(tmp_path):
    database_path = tmp_path / "observation.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        observations = load_appearance_observations(connection, live_run_id="live")

    assert observations[1].observation == STARTER
    assert observations[2].observation == SUBSTITUTE
    assert observations[3].observation == UNAVAILABLE
    assert observations[4].observation == UNUSED_SUBSTITUTE_OR_NOT_IN_SQUAD
    assert observations[5].observation == NO_TEAM_FIXTURE
    assert observations[6].observation == NOT_YET_ELIGIBLE
    assert len(observations) == 6


def test_missing_resolution_run_treats_eligibility_as_unresolved(tmp_path):
    database_path = tmp_path / "observation.duckdb"
    _seed(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute("DELETE FROM player_availability_resolution")
        connection.execute("DELETE FROM availability_resolution_run")

    with duckdb.connect(str(database_path), read_only=True) as connection:
        observations = load_appearance_observations(connection, live_run_id="live")

    # fpl_id 3 (previously UNAVAILABLE) falls back to the ambiguous bucket
    # once there is no resolution run to explain the 0 minutes.
    assert observations[3].observation == UNUSED_SUBSTITUTE_OR_NOT_IN_SQUAD


def test_rejects_a_provisional_not_yet_final_event_live_run(tmp_path):
    database_path = tmp_path / "observation.duckdb"
    _seed(database_path, event_finished=True, data_checked=False)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        with pytest.raises(ValueError, match="not final"):
            load_appearance_observations(connection, live_run_id="live")


def test_raises_on_unknown_live_run_id(tmp_path):
    database_path = tmp_path / "observation.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        with pytest.raises(ValueError, match="unknown live_run_id"):
            load_appearance_observations(connection, live_run_id="nope")
