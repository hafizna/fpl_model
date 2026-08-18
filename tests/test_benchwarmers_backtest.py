from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pandas as pd
import pytest

from fpl_model.ingest.player_history import import_player_fixture_history
from fpl_model.validation.backtest import score_predictions, walk_forward_folds
from fpl_model.validation.benchwarmers_backtest import (
    DOUBLE_GAMEWEEK_FIXTURE,
    materialize_benchwarmers_walk_forward_backtest,
)

# 4 teams (A, B, C, D), 5 GWs, one fixture per team per GW (A-B, C-D each week).
# One outfield player per team (all DEF, so DefCon/clean-sheet paths are hit) plus
# one goalkeeper for team A, so saves are exercised too.
TEAMS = {"A": 1, "B": 2, "C": 3, "D": 4}
PLAYERS = [
    # code, team, position, element (source_player_id)
    (5001, "A", "DEF", 1),
    (5002, "B", "DEF", 2),
    (5003, "C", "DEF", 3),
    (5004, "D", "DEF", 4),
    (5005, "A", "GK", 5),
]
KICKOFFS = {
    1: "2025-08-16T14:00:00Z",
    2: "2025-08-23T14:00:00Z",
    3: "2025-08-30T14:00:00Z",
    4: "2025-09-13T14:00:00Z",
    5: "2025-09-20T14:00:00Z",
}


_SEASON_TOTAL_COLUMNS = (
    "minutes",
    "starts",
    "expected_goals",
    "expected_assists",
    "saves",
    "yellow_cards",
    "red_cards",
    "bonus",
    "bps",
    "defensive_contribution",
)


def _players_raw(gameweeks: pd.DataFrame | None = None) -> pd.DataFrame:
    """Build players_raw.csv season totals that reconcile exactly with the
    given (or default) gameweek rows, matching build_player_fixture_history's
    own per-player sum-of-fixture-rows reconciliation check."""
    source = gameweeks if gameweeks is not None else _gameweeks()
    totals = source.groupby("code")[list(_SEASON_TOTAL_COLUMNS)].sum()
    rows = []
    for code, team, position, element in PLAYERS:
        element_type = {"GK": 1, "DEF": 2, "MID": 3, "FWD": 4}[position]
        row = {
            "id": element,
            "code": code,
            "web_name": f"Player{code}",
            "team": TEAMS[team],
            "element_type": element_type,
        }
        row.update(totals.loc[code].to_dict())
        rows.append(row)
    return pd.DataFrame(rows)


def _gameweeks(*, broken_player_code: int | None = None) -> pd.DataFrame:
    rows = []
    fixture_id = 100
    for gw in range(1, 6):
        for (home, away) in (("A", "B"), ("C", "D")):
            fixture_id += 1
            for team, was_home, opponent in ((home, True, TEAMS[away]), (away, False, TEAMS[home])):
                for code, player_team, position, element in PLAYERS:
                    if player_team != team:
                        continue
                    minutes = 90
                    if broken_player_code is not None and code == broken_player_code:
                        minutes = 0
                    rows.append(
                        {
                            "element": element,
                            "code": code,
                            "position": position,
                            "team": team,
                            "GW": gw,
                            "fixture": fixture_id,
                            "kickoff_time": KICKOFFS[gw],
                            "was_home": was_home,
                            "opponent_team": opponent,
                            "team_a_score": 1,
                            "team_h_score": 2,
                            "minutes": minutes,
                            "starts": 1 if minutes > 0 else 0,
                            "expected_goals": 0.1 if position != "GK" and minutes > 0 else 0.0,
                            "expected_assists": 0.05 if position != "GK" and minutes > 0 else 0.0,
                            "expected_goals_conceded": 1.0 if minutes > 0 else 0.0,
                            "goals_conceded": 1,
                            "saves": 4 if position == "GK" and minutes > 0 else 0,
                            "yellow_cards": 0,
                            "red_cards": 0,
                            "bonus": 1 if minutes > 0 else 0,
                            "bps": 20 if minutes > 0 else 0,
                            "defensive_contribution": 10 if position != "GK" and minutes > 0 else 0,
                            "total_points": 2 if minutes > 0 else 0,
                        }
                    )
    return pd.DataFrame(rows)


def _import(tmp_path, players=None, gameweeks=None):
    players_path = tmp_path / "players_raw.csv"
    gameweeks_path = tmp_path / "merged_gw.csv"
    gameweeks_frame = gameweeks if gameweeks is not None else _gameweeks()
    players_frame = players if players is not None else _players_raw(gameweeks_frame)
    players_frame.to_csv(players_path, index=False)
    gameweeks_frame.to_csv(gameweeks_path, index=False)
    database_path = tmp_path / "fpl.duckdb"
    timestamp = datetime(2025, 10, 1, 12, 0, tzinfo=UTC)
    result = import_player_fixture_history(
        players_path,
        gameweeks_path,
        season="2025-26",
        source_revision="abc123",
        source_committed_at=timestamp,
        imported_at=timestamp,
        database_path=database_path,
    )
    return result, database_path, players_frame, gameweeks_frame


def test_backtest_scores_eligible_rows_across_gameweeks(tmp_path):
    result, database_path, players_frame, gameweeks_frame = _import(tmp_path)

    with duckdb.connect(str(database_path)) as connection:
        backtest = materialize_benchwarmers_walk_forward_backtest(
            season="2025-26",
            import_run_id=result.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_frame,
            players_raw_frame=players_frame,
            evaluation_from_gw=3,
            evaluation_to_gw=4,
        )

    assert backtest.evaluated_gameweeks == (3, 4)
    # 5 players x 2 GWs = 10 candidate rows, all should score cleanly.
    assert backtest.candidate_player_fixture_rows == 10
    assert backtest.scored_player_fixture_rows == 10
    assert backtest.gaps == ()
    for observation in backtest.observations:
        assert observation.predicted_xpts >= 0.0
        assert observation.predicted_xpts == observation.predicted_xpts  # not NaN

    # Reused primitives must accept the driver's own observation shape.
    folds = walk_forward_folds(backtest.observations)
    assert len(folds) == 2
    metrics = score_predictions(backtest.observations)
    assert metrics.observations == 10


def test_backtest_diagnostics_match_observations_one_to_one(tmp_path):
    result, database_path, players_frame, gameweeks_frame = _import(tmp_path)

    with duckdb.connect(str(database_path)) as connection:
        backtest = materialize_benchwarmers_walk_forward_backtest(
            season="2025-26",
            import_run_id=result.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_frame,
            players_raw_frame=players_frame,
            evaluation_from_gw=3,
            evaluation_to_gw=4,
        )

    assert len(backtest.diagnostics) == len(backtest.observations)
    observation_keys = {
        (o.player_id, o.fixture_id, o.gameweek) for o in backtest.observations
    }
    diagnostics_keys = {
        (d.player_code, d.fixture_id, d.gameweek) for d in backtest.diagnostics
    }
    assert observation_keys == diagnostics_keys

    # Each diagnostic's predicted_xpts/actual_points must match its paired
    # observation exactly, and its position must match the seeded fixture.
    observation_by_key = {
        (o.player_id, o.fixture_id, o.gameweek): o for o in backtest.observations
    }
    for diagnostic in backtest.diagnostics:
        key = (diagnostic.player_code, diagnostic.fixture_id, diagnostic.gameweek)
        observation = observation_by_key[key]
        assert diagnostic.predicted_xpts == observation.predicted_xpts
        assert diagnostic.actual_points == observation.actual_points
        assert 0.0 <= diagnostic.start_probability <= 1.0
        assert diagnostic.expected_minutes >= 0.0
        expected_position = next(
            position for code, _, position, _ in PLAYERS if code == diagnostic.player_code
        )
        assert diagnostic.position == expected_position
        # The 11 components sum to the pre-home-away total, not predicted_xpts
        # itself (which has the fixture's home/away multiplier already
        # applied) -- see ScoredObservationDiagnostics' docstring.
        component_total = (
            diagnostic.component_appearance
            + diagnostic.component_sixty_minutes
            + diagnostic.component_saves
            + diagnostic.component_yellow_cards
            + diagnostic.component_red_cards
            + diagnostic.component_bonus
            + diagnostic.component_assists
            + diagnostic.component_goals
            + diagnostic.component_clean_sheet
            + diagnostic.component_goals_conceded
            + diagnostic.component_defcon
        )
        multiplier = diagnostic.predicted_xpts / component_total if component_total else 0.0
        assert multiplier == pytest.approx(1.05) or multiplier == pytest.approx(0.95)


def test_backtest_flags_zero_minute_history_as_a_gap(tmp_path):
    gameweeks = _gameweeks(broken_player_code=5001)
    result, database_path, players_frame, _ = _import(tmp_path, gameweeks=gameweeks)

    with duckdb.connect(str(database_path)) as connection:
        backtest = materialize_benchwarmers_walk_forward_backtest(
            season="2025-26",
            import_run_id=result.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks,
            players_raw_frame=players_frame,
            evaluation_from_gw=3,
            evaluation_to_gw=3,
        )

    broken_gaps = [gap for gap in backtest.gaps if gap.player_code == 5001]
    assert len(broken_gaps) == 1
    assert "NO_USABLE_PLAYER_RATE_HISTORY" in broken_gaps[0].flags
    assert broken_gaps[0].position == "DEF"
    assert not any(o.player_id == 5001 for o in backtest.observations)


def test_backtest_excludes_double_gameweek_players(tmp_path):
    gameweeks = _gameweeks()
    extra_fixture = gameweeks.loc[
        (gameweeks["GW"] == 3) & (gameweeks["code"] == 5001)
    ].copy()
    extra_fixture["fixture"] = 9999
    gameweeks = pd.concat([gameweeks, extra_fixture], ignore_index=True)
    result, database_path, players_frame, _ = _import(tmp_path, gameweeks=gameweeks)

    with duckdb.connect(str(database_path)) as connection:
        backtest = materialize_benchwarmers_walk_forward_backtest(
            season="2025-26",
            import_run_id=result.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks,
            players_raw_frame=players_frame,
            evaluation_from_gw=3,
            evaluation_to_gw=3,
        )

    dgw_gaps = [gap for gap in backtest.gaps if gap.player_code == 5001]
    assert len(dgw_gaps) == 2
    assert all(DOUBLE_GAMEWEEK_FIXTURE in gap.flags for gap in dgw_gaps)
    assert not any(o.player_id == 5001 for o in backtest.observations)


def test_backtest_gw5_prediction_is_unaffected_by_mutating_gw6_data(tmp_path):
    result, database_path, players_frame, gameweeks_frame = _import(tmp_path)
    with duckdb.connect(str(database_path)) as connection:
        before = materialize_benchwarmers_walk_forward_backtest(
            season="2025-26",
            import_run_id=result.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_frame,
            players_raw_frame=players_frame,
            evaluation_from_gw=5,
            evaluation_to_gw=5,
        )
    before_by_player = {o.player_id: o.predicted_xpts for o in before.observations}
    assert before_by_player  # sanity: something was actually scored

    # Extend the dataset with a GW6 that, if it leaked into GW5's prediction,
    # would drastically change every rate/appearance/team-strength input.
    gw6 = _gameweeks().loc[lambda f: f["GW"] == 5].copy()
    gw6["GW"] = 6
    gw6["fixture"] = gw6["fixture"] + 1000
    gw6["kickoff_time"] = "2025-09-27T14:00:00Z"
    gw6["expected_goals"] = 99.0
    gw6["expected_assists"] = 99.0
    gw6["bonus"] = 99
    gw6["total_points"] = 99
    mutated_gameweeks = pd.concat([_gameweeks(), gw6], ignore_index=True)
    mutated_dir = tmp_path / "mutated"
    mutated_dir.mkdir()
    mutated_result, mutated_database_path, mutated_players_frame, _ = _import(
        mutated_dir, gameweeks=mutated_gameweeks
    )
    with duckdb.connect(str(mutated_database_path)) as connection:
        after = materialize_benchwarmers_walk_forward_backtest(
            season="2025-26",
            import_run_id=mutated_result.import_run_id,
            connection=connection,
            gameweeks_frame=mutated_gameweeks,
            players_raw_frame=mutated_players_frame,
            evaluation_from_gw=5,
            evaluation_to_gw=5,
        )
    after_by_player = {o.player_id: o.predicted_xpts for o in after.observations}

    assert before_by_player == after_by_player


def test_backtest_gw5_prediction_is_unaffected_by_a_postponed_gw3_fixture(tmp_path):
    """gameweek < N alone is not deadline-safe: a fixture can be labelled with
    an earlier GW yet kick off (and have its outcome known) only after a
    later GW's deadline. Postpone team A-B's GW3 fixture to kick off after
    GW5's deadline, then confirm mutating that postponed fixture's data does
    not change GW5's prediction for team C/D players, whose rate windows
    would otherwise include the (still gameweek < 5) postponed row.
    """
    gameweeks = _gameweeks()
    # GW3's A-B fixture rows: team A's DEF + GK, plus team B's DEF.
    postponed_mask = (gameweeks["GW"] == 3) & (gameweeks["team"].isin(["A", "B"]))
    assert postponed_mask.sum() == 3
    # GW5 kicks off 2025-09-20T14:00:00Z; postpone well after its deadline
    # (2025-09-20T12:30:00Z = kickoff minus the 90-minute buffer).
    gameweeks.loc[postponed_mask, "kickoff_time"] = "2025-09-25T14:00:00Z"
    result, database_path, players_frame, gameweeks_frame = _import(
        tmp_path, gameweeks=gameweeks
    )
    with duckdb.connect(str(database_path)) as connection:
        before = materialize_benchwarmers_walk_forward_backtest(
            season="2025-26",
            import_run_id=result.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_frame,
            players_raw_frame=players_frame,
            evaluation_from_gw=5,
            evaluation_to_gw=5,
        )
    before_by_player = {o.player_id: o.predicted_xpts for o in before.observations}
    assert before_by_player  # sanity: something was actually scored

    # Mutate the postponed fixture's stats to an extreme value. If gameweek <
    # N were the only gate, this GW3-labelled row would already be inside
    # every GW5 rate/appearance/team-strength window and this mutation would
    # change GW5's predictions.
    mutated_gameweeks = gameweeks.copy()
    mutated_gameweeks.loc[postponed_mask, "expected_goals"] = 99.0
    mutated_gameweeks.loc[postponed_mask, "expected_assists"] = 99.0
    mutated_gameweeks.loc[postponed_mask, "bonus"] = 99
    mutated_gameweeks.loc[postponed_mask, "defensive_contribution"] = 99
    mutated_gameweeks.loc[postponed_mask, "total_points"] = 99
    mutated_dir = tmp_path / "mutated"
    mutated_dir.mkdir()
    mutated_result, mutated_database_path, mutated_players_frame, mutated_frame = _import(
        mutated_dir, gameweeks=mutated_gameweeks
    )
    with duckdb.connect(str(mutated_database_path)) as connection:
        after = materialize_benchwarmers_walk_forward_backtest(
            season="2025-26",
            import_run_id=mutated_result.import_run_id,
            connection=connection,
            gameweeks_frame=mutated_frame,
            players_raw_frame=mutated_players_frame,
            evaluation_from_gw=5,
            evaluation_to_gw=5,
        )
    after_by_player = {o.player_id: o.predicted_xpts for o in after.observations}

    assert before_by_player == after_by_player


def test_backtest_rejects_invalid_evaluation_range(tmp_path):
    result, database_path, players_frame, gameweeks_frame = _import(tmp_path)
    with duckdb.connect(str(database_path)) as connection:
        with pytest.raises(ValueError, match="evaluation_from_gw"):
            materialize_benchwarmers_walk_forward_backtest(
                season="2025-26",
                import_run_id=result.import_run_id,
                connection=connection,
                gameweeks_frame=gameweeks_frame,
                players_raw_frame=players_frame,
                evaluation_from_gw=10,
                evaluation_to_gw=3,
            )
