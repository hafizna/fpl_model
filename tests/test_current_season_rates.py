from __future__ import annotations

import json
from datetime import UTC, datetime

import duckdb
import pytest

from fpl_model.model.current_season_rates import (
    NO_PREVIOUS_SEASON_RATE_HISTORY,
    SHRINKAGE_PRIOR_MINUTES,
    ZERO_CURRENT_SEASON_MINUTES,
    materialize_current_season_rates,
)
from fpl_model.storage import initialize_database


def _seed(database_path) -> None:
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('snapshot', 'fpl_api', '2026-08-18T09:00:00+00:00', 'completed');

            -- player 1 (code 1001): has previous-season rate history ->
            -- shrinkage toward their own prior.
            -- player 2 (code 1002): NO previous-season rate history ->
            -- raw current-season rate, flagged.
            -- player 3 (code 1003): has previous-season history but ZERO
            -- current-season minutes -> zero-minute flag, blended rate
            -- collapses fully to the prior (weight = 0).
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES
                ('snapshot', '2026-27', 1, 1001, 'A', 'Established', 'Established', 1, 'MID', 7.0, 'a'),
                ('snapshot', '2026-27', 2, 1002, 'B', 'New', 'New', 1, 'MID', 5.5, 'a'),
                ('snapshot', '2026-27', 3, 1003, 'C', 'Benched', 'Benched', 1, 'DEF', 4.5, 'a');

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
            ) VALUES ('rates', 'fixture_history', 38, 6, 10, 'test', 2, 'completed');
            INSERT INTO player_rate_history VALUES
                ('rates', 1001, 'Established', 'MID', 2850, 30, 0, 3, 0, 8, 350,
                 2850, 9.0, 6.0, 2850, 9.0, 6.0, 0, 20, 0, 20, '[]'),
                ('rates', 1003, 'Benched', 'DEF', 1800, 18, 0, 1, 0, 2, 100,
                 1800, 1.8, 0.9, 1800, 1.8, 0.9, 1800, 36, 1800, 36, '[]');

            -- Two Gameweeks of official current-season live data: GW1 final,
            -- GW2 still provisional (must be excluded from a GW3 rate window).
            INSERT INTO fpl_event_live_run VALUES (
                'live_gw1', 'snapshot', '2026-27', 1, '2026-08-24T20:00:00+00:00',
                'gw1.json', 'sha1', TRUE, TRUE, 3, 'completed', current_timestamp
            );
            INSERT INTO fpl_event_live_run VALUES (
                'live_gw2', 'snapshot', '2026-27', 2, '2026-08-31T20:00:00+00:00',
                'gw2.json', 'sha2', TRUE, FALSE, 3, 'provisional', current_timestamp
            );

            INSERT INTO player_gameweek_stat VALUES
                -- GW1 (final): player 1 started and scored heavily above
                -- their own prior rate; player 3 did not play.
                ('live_gw1', 1, 1001, TRUE, 90, 1, 2, 1, 0, 0, 0, 12, 45, 3,
                 1.1, 0.5, 0.2, 14, FALSE, '[]'),
                ('live_gw1', 3, 1003, FALSE, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                 0.0, 0.0, 0.0, 0, FALSE, '[]'),
                -- GW2 (provisional): must be excluded from any GW3 window.
                ('live_gw2', 1, 1001, TRUE, 90, 1, 1, 0, 0, 0, 0, 10, 40, 2,
                 0.5, 0.2, 0.1, 8, FALSE, '[]');
            """
        )


def test_materializes_shrunk_rates_using_only_final_gameweeks(tmp_path):
    database_path = tmp_path / "rates.duckdb"
    _seed(database_path)

    result = materialize_current_season_rates(
        source_ingestion_run_id="snapshot",
        season="2026-27",
        as_of_gameweek=3,
        as_of=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
        database_path=database_path,
    )

    # GW2's live run is provisional -> excluded even though gameweek < 3.
    assert result.final_gameweeks == (1,)
    assert result.player_rows == 2  # only players with a player_gameweek_stat row

    with duckdb.connect(str(database_path), read_only=True) as connection:
        rows = {
            row[0]: row
            for row in connection.execute(
                "SELECT player_code, current_season_minutes, current_season_starts, "
                "shrunk_expected_goals_per_90, shrunk_expected_assists_per_90, "
                "shrunk_defensive_contribution_per_90, shrunk_saves_per_90, "
                "prior_source, data_quality_flags "
                "FROM current_season_player_rate WHERE rate_run_id = ?",
                [result.rate_run_id],
            ).fetchall()
        }

    # Player 1001: 90 current-season minutes at a 1.1 xG rate (1.1 xG per 90
    # minutes played), blended toward their own prior of 9.0 long-form xG /
    # 2850 long-form minutes = 0.2842.. xG per 90.
    established = rows[1001]
    current_xg_per_90 = 1.1 / 90.0 * 90.0
    prior_xg_per_90 = 9.0 / 2850.0 * 90.0
    weight = 90.0 / (90.0 + SHRINKAGE_PRIOR_MINUTES)
    expected_shrunk_xg = weight * current_xg_per_90 + (1.0 - weight) * prior_xg_per_90
    assert established[1] == 90  # only GW1 counted, not the provisional GW2
    assert established[3] == pytest.approx(expected_shrunk_xg)
    assert established[7] == "previous_season_player_rate"
    assert json.loads(established[8]) == ["NO_SAVES_SHRINKAGE_PRIOR"]

    # Player 1003: has a previous-season prior but ZERO current-season
    # minutes in the eligible window -> weight is exactly 0, blended rate
    # collapses fully to the prior, and the zero-minutes flag is present.
    benched = rows[1003]
    prior_defcon_per_90 = 36.0 / 1800.0 * 90.0
    assert benched[1] == 0
    assert benched[5] == pytest.approx(prior_defcon_per_90)
    assert ZERO_CURRENT_SEASON_MINUTES in json.loads(benched[8])


def test_player_with_no_previous_season_history_gets_raw_current_rate_and_flag(tmp_path):
    database_path = tmp_path / "rates.duckdb"
    _seed(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO player_gameweek_stat VALUES (
                'live_gw1', 2, 1002, TRUE, 45, 0, 1, 0, 0, 0, 0, 5, 20, 1,
                0.4, 0.1, 0.05, 5, FALSE, '[]'
            )
            """
        )

    result = materialize_current_season_rates(
        source_ingestion_run_id="snapshot",
        season="2026-27",
        as_of_gameweek=3,
        as_of=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
        database_path=database_path,
    )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        row = connection.execute(
            "SELECT shrunk_expected_goals_per_90, prior_source, data_quality_flags "
            "FROM current_season_player_rate WHERE rate_run_id = ? AND player_code = 1002",
            [result.rate_run_id],
        ).fetchone()

    shrunk_xg, prior_source, flags_json = row
    # No shrinkage possible: the raw current-season per-90 rate is used as-is.
    assert shrunk_xg == pytest.approx(0.4 / 45.0 * 90.0)
    assert prior_source == "no_previous_season_history"
    assert NO_PREVIOUS_SEASON_RATE_HISTORY in json.loads(flags_json)


def test_no_final_gameweeks_yields_an_empty_completed_run(tmp_path):
    database_path = tmp_path / "rates.duckdb"
    _seed(database_path)

    result = materialize_current_season_rates(
        source_ingestion_run_id="snapshot",
        season="2026-27",
        as_of_gameweek=1,
        as_of=datetime(2026, 8, 20, 0, 0, tzinfo=UTC),
        database_path=database_path,
    )

    assert result.final_gameweeks == ()
    assert result.player_rows == 0
    assert result.status == "completed"


def test_a_gameweek_finalised_after_as_of_is_excluded(tmp_path):
    database_path = tmp_path / "rates.duckdb"
    _seed(database_path)
    with duckdb.connect(str(database_path)) as connection:
        # GW2 becomes final, but only AFTER the as_of timestamp used below.
        connection.execute(
            "UPDATE fpl_event_live_run SET event_finished = TRUE, data_checked = TRUE, "
            "status = 'completed' WHERE live_run_id = 'live_gw2'"
        )

    result = materialize_current_season_rates(
        source_ingestion_run_id="snapshot",
        season="2026-27",
        as_of_gameweek=3,
        as_of=datetime(2026, 8, 25, 0, 0, tzinfo=UTC),  # before live_gw2's captured_at
        database_path=database_path,
    )

    assert result.final_gameweeks == (1,)


def test_repeated_call_with_the_same_inputs_is_idempotent(tmp_path):
    database_path = tmp_path / "rates.duckdb"
    _seed(database_path)
    as_of = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)

    first = materialize_current_season_rates(
        source_ingestion_run_id="snapshot",
        season="2026-27",
        as_of_gameweek=3,
        as_of=as_of,
        database_path=database_path,
    )
    second = materialize_current_season_rates(
        source_ingestion_run_id="snapshot",
        season="2026-27",
        as_of_gameweek=3,
        as_of=as_of,
        database_path=database_path,
    )

    assert first.rate_run_id == second.rate_run_id
    assert first.final_gameweeks == second.final_gameweeks
    assert first.player_rows == second.player_rows
    with duckdb.connect(str(database_path), read_only=True) as connection:
        count = connection.execute(
            "SELECT count(*) FROM current_season_player_rate WHERE rate_run_id = ?",
            [first.rate_run_id],
        ).fetchone()[0]
    assert count == first.player_rows  # no duplicate rows from the second call


def test_rejects_naive_as_of(tmp_path):
    database_path = tmp_path / "rates.duckdb"
    _seed(database_path)

    with pytest.raises(ValueError, match="timezone-aware"):
        materialize_current_season_rates(
            source_ingestion_run_id="snapshot",
            season="2026-27",
            as_of_gameweek=3,
            as_of=datetime(2026, 9, 1, 0, 0),
            database_path=database_path,
        )


def test_rejects_out_of_range_gameweek(tmp_path):
    database_path = tmp_path / "rates.duckdb"
    _seed(database_path)

    with pytest.raises(ValueError, match="as_of_gameweek"):
        materialize_current_season_rates(
            source_ingestion_run_id="snapshot",
            season="2026-27",
            as_of_gameweek=0,
            as_of=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
            database_path=database_path,
        )
