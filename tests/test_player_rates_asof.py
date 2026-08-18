from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pandas as pd
import pytest

from fpl_model.ingest.player_history import import_player_fixture_history
from fpl_model.validation.player_rates_asof import (
    has_usable_rate_history,
    league_average_bonus_rates_as_of,
    player_rates_as_of,
)


def _players() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": 1,
                "code": 1001,
                "web_name": "Forward",
                "team": 1,
                "element_type": 4,
                "minutes": 270,
                "starts": 3,
                "expected_goals": 1.8,
                "expected_assists": 0.3,
                "saves": 0,
                "yellow_cards": 1,
                "red_cards": 0,
                "bonus": 4,
                "bps": 60,
                "defensive_contribution": 12,
            },
            {
                "id": 2,
                "code": 2002,
                "web_name": "Keeper",
                "team": 2,
                "element_type": 1,
                "minutes": 0,
                "starts": 0,
                "expected_goals": 0.0,
                "expected_assists": 0.0,
                "saves": 0,
                "yellow_cards": 0,
                "red_cards": 0,
                "bonus": 0,
                "bps": 0,
                "defensive_contribution": 0,
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
            "expected_goals": 0.6,
            "expected_assists": 0.1,
            "saves": 0,
            "yellow_cards": 0,
            "red_cards": 0,
            "bonus": 1,
            "bps": 20,
            "defensive_contribution": 4,
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
            "minutes": 90,
            "starts": 1,
            "expected_goals": 0.5,
            "expected_assists": 0.1,
            "saves": 0,
            "yellow_cards": 1,
            "red_cards": 0,
            "bonus": 1,
            "bps": 18,
            "defensive_contribution": 4,
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
            "expected_goals": 0.7,
            "expected_assists": 0.1,
            "saves": 0,
            "yellow_cards": 0,
            "red_cards": 0,
            "bonus": 2,
            "bps": 22,
            "defensive_contribution": 4,
        },
        {
            "element": 2,
            "position": "GK",
            "team": "CCC",
            "GW": 1,
            "fixture": 103,
            "kickoff_time": "2025-08-16T14:00:00Z",
            "was_home": True,
            "opponent_team": 5,
            "minutes": 0,
            "starts": 0,
            "expected_goals": 0.0,
            "expected_assists": 0.0,
            "saves": 0,
            "yellow_cards": 0,
            "red_cards": 0,
            "bonus": 0,
            "bps": 0,
            "defensive_contribution": 0,
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


def test_player_rates_as_of_uses_only_prior_gameweeks(tmp_path):
    result, database_path = _import(tmp_path)

    with duckdb.connect(str(database_path)) as connection:
        rates = player_rates_as_of(
            connection,
            import_run_id=result.import_run_id,
            as_of_gameweek=3,
            short_form_gameweeks=6,
            defcon_short_form_gameweeks=10,
        )

    forward = rates[1001]
    # GW < 3 => GW1, GW2 only.
    assert forward.long_form_minutes == 180
    assert forward.long_form_expected_goals == pytest.approx(1.1)
    assert forward.short_form_minutes == 180
    assert forward.short_form_expected_goals == pytest.approx(1.1)
    # season_* fields are also causal (gameweek < as_of_gameweek), not full-season.
    assert forward.season_starts == 2
    assert forward.data_quality_flags == ()
    assert has_usable_rate_history(forward)

    keeper = rates[2002]
    assert keeper.long_form_minutes == 0
    assert keeper.season_minutes == 0
    assert "ZERO_LONG_FORM_MINUTES" in keeper.data_quality_flags
    assert "ZERO_PRIOR_STARTS" in keeper.data_quality_flags
    assert not has_usable_rate_history(keeper)


def test_player_rates_as_of_is_causally_unaffected_by_future_gameweeks(tmp_path):
    result, database_path = _import(tmp_path)
    with duckdb.connect(str(database_path)) as connection:
        before = player_rates_as_of(
            connection, import_run_id=result.import_run_id, as_of_gameweek=2
        )

    mutated_gameweeks = _gameweeks()
    mutated_gameweeks.loc[mutated_gameweeks["GW"] == 3, "expected_goals"] = 9.9
    mutated_players = _players()
    # Keep players_raw.csv's season total reconciled with the mutated GW3 row.
    mutated_players.loc[mutated_players["code"] == 1001, "expected_goals"] = (
        0.6 + 0.5 + 9.9
    )
    mutated_dir = tmp_path / "mutated"
    mutated_dir.mkdir()
    mutated_result, mutated_database_path = _import(
        mutated_dir, players=mutated_players, gameweeks=mutated_gameweeks
    )
    with duckdb.connect(str(mutated_database_path)) as connection:
        after = player_rates_as_of(
            connection, import_run_id=mutated_result.import_run_id, as_of_gameweek=2
        )

    assert before[1001] == after[1001]


def test_league_average_bonus_rates_as_of_uses_only_prior_gameweeks(tmp_path):
    result, database_path = _import(tmp_path)

    with duckdb.connect(str(database_path)) as connection:
        avg_bps, avg_bonus, avg_bonus_per_bps = league_average_bonus_rates_as_of(
            connection, import_run_id=result.import_run_id, as_of_gameweek=3
        )

    # GW1+GW2 starts=2, bonus=1+1=2, bps=20+18=38.
    assert avg_bonus == pytest.approx(1.0)
    assert avg_bps == pytest.approx(19.0)
    assert avg_bonus_per_bps == pytest.approx(2.0 / 38.0)


def test_league_average_bonus_rates_as_of_rejects_no_prior_starts(tmp_path):
    result, database_path = _import(tmp_path)

    with duckdb.connect(str(database_path)) as connection:
        with pytest.raises(ValueError, match="no starts recorded"):
            league_average_bonus_rates_as_of(
                connection, import_run_id=result.import_run_id, as_of_gameweek=1
            )


def test_player_rates_as_of_rejects_out_of_range_gameweek(tmp_path):
    result, database_path = _import(tmp_path)
    with duckdb.connect(str(database_path)) as connection:
        with pytest.raises(ValueError, match="as_of_gameweek"):
            player_rates_as_of(
                connection, import_run_id=result.import_run_id, as_of_gameweek=0
            )


def test_player_rates_as_of_excludes_postponed_fixture_via_target_deadline(tmp_path):
    # Postpone GW2's row to kick off after GW3's inferred deadline
    # (2025-08-30T12:30:00Z = GW3 kickoff minus the 90-minute buffer).
    # gameweek < 3 alone would still include it; target_deadline must not.
    postponed = _gameweeks()
    postponed.loc[postponed["GW"] == 2, "kickoff_time"] = "2025-09-01T14:00:00Z"
    result, database_path = _import(tmp_path, gameweeks=postponed)
    target_deadline = datetime(2025, 8, 30, 12, 30, tzinfo=UTC)

    with duckdb.connect(str(database_path)) as connection:
        without_gate = player_rates_as_of(
            connection, import_run_id=result.import_run_id, as_of_gameweek=3
        )
        with_gate = player_rates_as_of(
            connection,
            import_run_id=result.import_run_id,
            as_of_gameweek=3,
            target_deadline=target_deadline,
        )

    # Without a deadline gate, the postponed GW2 row still counts (gameweek < 3).
    assert without_gate[1001].long_form_minutes == 180
    # With the gate, only GW1 remains causally available.
    assert with_gate[1001].long_form_minutes == 90
    assert with_gate[1001].long_form_expected_goals == pytest.approx(0.6)


def test_league_average_bonus_rates_as_of_excludes_postponed_fixture(tmp_path):
    postponed = _gameweeks()
    postponed.loc[postponed["GW"] == 2, "kickoff_time"] = "2025-09-01T14:00:00Z"
    result, database_path = _import(tmp_path, gameweeks=postponed)
    target_deadline = datetime(2025, 8, 30, 12, 30, tzinfo=UTC)

    with duckdb.connect(str(database_path)) as connection:
        avg_bps_without_gate, _, _ = league_average_bonus_rates_as_of(
            connection, import_run_id=result.import_run_id, as_of_gameweek=3
        )
        avg_bps_with_gate, _, _ = league_average_bonus_rates_as_of(
            connection,
            import_run_id=result.import_run_id,
            as_of_gameweek=3,
            target_deadline=target_deadline,
        )

    # Without the gate: GW1+GW2 starts=2, bps=20+18=38 => avg 19.0.
    assert avg_bps_without_gate == pytest.approx(19.0)
    # With the gate, the postponed GW2 row is excluded: GW1 only, bps=20/1=20.0.
    assert avg_bps_with_gate == pytest.approx(20.0)


def test_player_rates_as_of_rejects_naive_target_deadline(tmp_path):
    result, database_path = _import(tmp_path)
    with duckdb.connect(str(database_path)) as connection:
        with pytest.raises(ValueError, match="target_deadline"):
            player_rates_as_of(
                connection,
                import_run_id=result.import_run_id,
                as_of_gameweek=3,
                target_deadline=datetime(2025, 8, 30, 12, 30),  # naive, no tzinfo
            )
