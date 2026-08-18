from __future__ import annotations

import pandas as pd
import pytest

from fpl_model.validation.team_fixture_results import (
    build_team_fixture_results,
    build_team_name_to_id,
)


def _gameweek_rows() -> list[dict[str, object]]:
    # Fixture 1, GW1: Home (2 players) 2-1 Away (1 player).
    return [
        {
            "element": 1,
            "team": "Home",
            "GW": 1,
            "fixture": 1,
            "kickoff_time": "2025-08-16T14:00:00Z",
            "was_home": True,
            "opponent_team": 2,
            "team_a_score": 1,
            "team_h_score": 2,
            "minutes": 90,
            "expected_goals": 1.2,
            "expected_goals_conceded": 0.9,
        },
        {
            "element": 2,
            "team": "Home",
            "GW": 1,
            "fixture": 1,
            "kickoff_time": "2025-08-16T14:00:00Z",
            "was_home": True,
            "opponent_team": 2,
            "team_a_score": 1,
            "team_h_score": 2,
            "minutes": 60,
            "expected_goals": 0.4,
            # Partial-match value; must not be selected over the 90-minute row.
            "expected_goals_conceded": 0.5,
        },
        {
            "element": 3,
            "team": "Away",
            "GW": 1,
            "fixture": 1,
            "kickoff_time": "2025-08-16T14:00:00Z",
            "was_home": False,
            "opponent_team": 1,
            "team_a_score": 1,
            "team_h_score": 2,
            "minutes": 90,
            "expected_goals": 0.9,
            "expected_goals_conceded": 1.6,
        },
    ]


def test_build_team_fixture_results_derives_one_row_per_team_fixture():
    frame = pd.DataFrame(_gameweek_rows())

    result = build_team_fixture_results(frame)

    assert list(result["team"]) == ["Away", "Home"]
    home = result.loc[result["team"] == "Home"].iloc[0]
    assert home["team_goals_for"] == 2
    assert home["team_goals_against"] == 1
    assert bool(home["was_home"]) is True
    assert home["opponent_team_id"] == 2
    # Sum of expected_goals across both Home rows.
    assert home["team_xg_for"] == pytest.approx(1.6)
    # xGC taken from the minutes==90 row, not the 60-minute row.
    assert home["team_xg_against"] == pytest.approx(0.9)
    assert bool(home["partial_match_xgc_sample"]) is False

    away = result.loc[result["team"] == "Away"].iloc[0]
    assert away["team_goals_for"] == 1
    assert away["team_goals_against"] == 2
    assert bool(away["was_home"]) is False
    assert away["team_xg_against"] == pytest.approx(1.6)


def test_build_team_fixture_results_rejects_conflicting_team_fixture_facts():
    rows = _gameweek_rows()
    rows[1]["team_h_score"] = 3  # Conflicts with rows[0]'s team_h_score of 2.
    frame = pd.DataFrame(rows)

    with pytest.raises(ValueError, match="conflicting"):
        build_team_fixture_results(frame)


def test_build_team_fixture_results_rejects_missing_columns():
    with pytest.raises(ValueError, match="missing columns"):
        build_team_fixture_results(pd.DataFrame({"team": ["Home"]}))


def test_build_team_fixture_results_rejects_empty_frame():
    frame = pd.DataFrame(columns=list(_gameweek_rows()[0]))
    with pytest.raises(ValueError, match="must not be empty"):
        build_team_fixture_results(frame)


def test_build_team_name_to_id_resolves_stable_mapping():
    players_raw = pd.DataFrame({"id": [1, 2, 3], "team": [10, 10, 20]})
    gameweeks = pd.DataFrame(
        {
            "element": [1, 2, 3, 1],
            "team": ["Home", "Home", "Away", "Home"],
        }
    )

    mapping = build_team_name_to_id(players_raw, gameweeks)

    assert mapping == {"Home": 10, "Away": 20}


def test_build_team_name_to_id_resolves_transferred_player_by_majority_vote():
    # players_raw reflects each player's *current* team; element 3 has rows
    # under "Home" from before a mid-season transfer to team 20, while every
    # other "Home" row (elements 1, 2) still belongs to team 10 -- the
    # majority (by row count) must win, not the transferred player's row.
    players_raw = pd.DataFrame({"id": [1, 2, 3], "team": [10, 10, 20]})
    gameweeks = pd.DataFrame(
        {
            "element": [1, 1, 2, 2, 3],
            "team": ["Home", "Home", "Home", "Home", "Home"],
        }
    )

    mapping = build_team_name_to_id(players_raw, gameweeks)

    assert mapping == {"Home": 10}


def test_build_team_name_to_id_rejects_one_id_mapping_to_two_names():
    players_raw = pd.DataFrame({"id": [1, 2], "team": [10, 10]})
    gameweeks = pd.DataFrame({"element": [1, 2], "team": ["Home", "Away"]})

    with pytest.raises(ValueError, match="more than one team name"):
        build_team_name_to_id(players_raw, gameweeks)
