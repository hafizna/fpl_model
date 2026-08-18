from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pandas as pd
import pytest

from fpl_model.ingest.player_history import import_player_fixture_history
from fpl_model.validation.appearance_asof import (
    appearance_as_of,
    build_minutes_scenarios,
)


def test_build_minutes_scenarios_builds_equal_probability_distribution():
    scenarios = build_minutes_scenarios([90, 20, 0], [True, False, False])

    assert len(scenarios) == 3
    assert sum(scenario.probability for scenario in scenarios) == pytest.approx(1.0)
    assert scenarios[0] == pytest.approx(scenarios[0])  # sanity: dataclass equality
    assert [s.minutes for s in scenarios] == [90.0, 20.0, 0.0]
    assert [s.started for s in scenarios] == [True, False, False]


def test_build_minutes_scenarios_rejects_empty_input():
    with pytest.raises(ValueError, match="at least one fixture row"):
        build_minutes_scenarios([], [])


def test_build_minutes_scenarios_rejects_mismatched_lengths():
    with pytest.raises(ValueError, match="same length"):
        build_minutes_scenarios([90], [True, False])


def _players() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": 1,
                "code": 1001,
                "web_name": "Forward",
                "team": 1,
                "element_type": 4,
                "minutes": 200,
                "starts": 2,
                "expected_goals": 1.0,
                "expected_assists": 0.2,
                "saves": 0,
                "yellow_cards": 0,
                "red_cards": 0,
                "bonus": 2,
                "bps": 30,
                "defensive_contribution": 6,
            },
        ]
    )


def _gameweeks() -> pd.DataFrame:
    rows = [
        {
            "element": 1,
            "position": "FWD",
            "team": "AAA",
            "GW": 1,
            "fixture": 100,
            "kickoff_time": "2025-08-16T14:00:00Z",
            "was_home": True,
            "opponent_team": 2,
            "minutes": 90,
            "starts": 1,
            "expected_goals": 0.5,
            "expected_assists": 0.1,
            "saves": 0,
            "yellow_cards": 0,
            "red_cards": 0,
            "bonus": 1,
            "bps": 15,
            "defensive_contribution": 3,
        },
        {
            "element": 1,
            "position": "FWD",
            "team": "AAA",
            "GW": 2,
            "fixture": 101,
            "kickoff_time": "2025-08-23T14:00:00Z",
            "was_home": False,
            "opponent_team": 3,
            "minutes": 20,
            "starts": 0,
            "expected_goals": 0.2,
            "expected_assists": 0.0,
            "saves": 0,
            "yellow_cards": 0,
            "red_cards": 0,
            "bonus": 0,
            "bps": 5,
            "defensive_contribution": 1,
        },
        {
            "element": 1,
            "position": "FWD",
            "team": "AAA",
            "GW": 3,
            "fixture": 102,
            "kickoff_time": "2025-08-30T14:00:00Z",
            "was_home": True,
            "opponent_team": 4,
            "minutes": 90,
            "starts": 1,
            "expected_goals": 0.3,
            "expected_assists": 0.1,
            "saves": 0,
            "yellow_cards": 0,
            "red_cards": 0,
            "bonus": 1,
            "bps": 10,
            "defensive_contribution": 2,
        },
    ]
    return pd.DataFrame(rows)


def _import(tmp_path, players=None, gameweeks=None):
    players_path = tmp_path / "players_raw.csv"
    gameweeks_path = tmp_path / "merged_gw.csv"
    (players if players is not None else _players()).to_csv(players_path, index=False)
    (gameweeks if gameweeks is not None else _gameweeks()).to_csv(
        gameweeks_path, index=False
    )
    database_path = tmp_path / "fpl.duckdb"
    timestamp = datetime(2025, 9, 1, 12, 0, tzinfo=UTC)
    result = import_player_fixture_history(
        players_path,
        gameweeks_path,
        season="2025-26",
        source_revision="abc123",
        source_committed_at=timestamp,
        imported_at=timestamp,
        database_path=database_path,
    )
    return result, database_path


def test_appearance_as_of_uses_only_prior_gameweeks(tmp_path):
    result, database_path = _import(tmp_path)

    with duckdb.connect(str(database_path)) as connection:
        appearances = appearance_as_of(
            connection, import_run_id=result.import_run_id, as_of_gameweek=3
        )

    forward = appearances[1001]
    assert forward.fixtures_considered == 2  # GW1, GW2 only
    # start_probability = 1/2 (started in GW1, not GW2).
    assert forward.appearance.start_probability == pytest.approx(0.5)
    assert forward.appearance.expected_minutes == pytest.approx((90 + 20) / 2)


def test_appearance_as_of_omits_player_with_no_prior_fixtures(tmp_path):
    result, database_path = _import(tmp_path)

    with duckdb.connect(str(database_path)) as connection:
        appearances = appearance_as_of(
            connection, import_run_id=result.import_run_id, as_of_gameweek=1
        )

    assert appearances == {}


def test_appearance_as_of_respects_trailing_window(tmp_path):
    result, database_path = _import(tmp_path)

    with duckdb.connect(str(database_path)) as connection:
        appearances = appearance_as_of(
            connection,
            import_run_id=result.import_run_id,
            as_of_gameweek=3,
            window_gameweeks=1,
        )

    forward = appearances[1001]
    # Trailing 1 GW before GW3 => GW2 only.
    assert forward.fixtures_considered == 1
    assert forward.appearance.expected_minutes == pytest.approx(20.0)


def test_appearance_as_of_is_causally_unaffected_by_future_gameweeks(tmp_path):
    result, database_path = _import(tmp_path)
    with duckdb.connect(str(database_path)) as connection:
        before = appearance_as_of(
            connection, import_run_id=result.import_run_id, as_of_gameweek=2
        )

    mutated_gameweeks = _gameweeks()
    mutated_gameweeks.loc[mutated_gameweeks["GW"] == 3, "minutes"] = 0
    mutated_gameweeks.loc[mutated_gameweeks["GW"] == 3, "starts"] = 0
    mutated_players = _players()
    mutated_players.loc[mutated_players["code"] == 1001, "minutes"] = 90 + 20 + 0
    mutated_players.loc[mutated_players["code"] == 1001, "starts"] = 1
    mutated_dir = tmp_path / "mutated"
    mutated_dir.mkdir()
    mutated_result, mutated_database_path = _import(
        mutated_dir, players=mutated_players, gameweeks=mutated_gameweeks
    )
    with duckdb.connect(str(mutated_database_path)) as connection:
        after = appearance_as_of(
            connection, import_run_id=mutated_result.import_run_id, as_of_gameweek=2
        )

    assert before[1001] == after[1001]


def test_appearance_as_of_rejects_out_of_range_gameweek(tmp_path):
    result, database_path = _import(tmp_path)
    with duckdb.connect(str(database_path)) as connection:
        with pytest.raises(ValueError, match="as_of_gameweek"):
            appearance_as_of(
                connection, import_run_id=result.import_run_id, as_of_gameweek=0
            )


def test_appearance_as_of_excludes_postponed_fixture_via_target_deadline(tmp_path):
    # Postpone GW2's row (20-minute cameo) to kick off after GW3's inferred
    # deadline (2025-08-30T12:30:00Z = GW3 kickoff minus the 90-minute
    # buffer). gameweek < 3 alone would still include it.
    postponed = _gameweeks()
    postponed.loc[postponed["GW"] == 2, "kickoff_time"] = "2025-09-01T14:00:00Z"
    result, database_path = _import(tmp_path, gameweeks=postponed)
    target_deadline = datetime(2025, 8, 30, 12, 30, tzinfo=UTC)

    with duckdb.connect(str(database_path)) as connection:
        without_gate = appearance_as_of(
            connection, import_run_id=result.import_run_id, as_of_gameweek=3
        )
        with_gate = appearance_as_of(
            connection,
            import_run_id=result.import_run_id,
            as_of_gameweek=3,
            target_deadline=target_deadline,
        )

    assert without_gate[1001].fixtures_considered == 2  # GW1 + postponed GW2
    assert with_gate[1001].fixtures_considered == 1  # GW1 only
    assert with_gate[1001].appearance.expected_minutes == pytest.approx(90.0)


def test_appearance_as_of_rejects_naive_target_deadline(tmp_path):
    result, database_path = _import(tmp_path)
    with duckdb.connect(str(database_path)) as connection:
        with pytest.raises(ValueError, match="target_deadline"):
            appearance_as_of(
                connection,
                import_run_id=result.import_run_id,
                as_of_gameweek=3,
                target_deadline=datetime(2025, 8, 30, 12, 30),  # naive
            )
