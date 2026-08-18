from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pandas as pd
import pytest

from fpl_model.ingest.player_history import (
    build_player_fixture_history,
    import_player_fixture_history,
    materialize_preseason_rate_history,
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
                "minutes": 170,
                "starts": 2,
                "expected_goals": 1.5,
                "expected_assists": 0.3,
                "saves": 0,
                "yellow_cards": 1,
                "red_cards": 0,
                "bonus": 3,
                "bps": 40,
                "defensive_contribution": 18,
            },
            {
                "id": 2,
                "code": 2002,
                "web_name": "Keeper",
                "team": 2,
                "element_type": 1,
                "minutes": 90,
                "starts": 1,
                "expected_goals": 0.0,
                "expected_assists": 0.0,
                "saves": 4,
                "yellow_cards": 0,
                "red_cards": 0,
                "bonus": 1,
                "bps": 25,
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
            "GW": 32,
            "fixture": 320,
            "kickoff_time": "2026-04-01T14:00:00Z",
            "was_home": True,
            "opponent_team": 2,
            "minutes": 90,
            "starts": 1,
            "expected_goals": 1.0,
            "expected_assists": 0.1,
            "saves": 0,
            "yellow_cards": 0,
            "red_cards": 0,
            "bonus": 2,
            "bps": 25,
            "defensive_contribution": 8,
        },
        {
            "element": 1,
            "position": "FWD",
            "team": "BBB",
            "GW": 33,
            "fixture": 330,
            "kickoff_time": "2026-04-08T14:00:00Z",
            "was_home": False,
            "opponent_team": 3,
            "minutes": 80,
            "starts": 1,
            "expected_goals": 0.5,
            "expected_assists": 0.2,
            "saves": 0,
            "yellow_cards": 1,
            "red_cards": 0,
            "bonus": 1,
            "bps": 15,
            "defensive_contribution": 10,
        },
        {
            "element": 2,
            "position": "GK",
            "team": "BBB",
            "GW": 38,
            "fixture": 380,
            "kickoff_time": "2026-05-20T14:00:00Z",
            "was_home": "true",
            "opponent_team": 1,
            "minutes": 90,
            "starts": 1,
            "expected_goals": 0.0,
            "expected_assists": 0.0,
            "saves": 4,
            "yellow_cards": 0,
            "red_cards": 0,
            "bonus": 1,
            "bps": 25,
            "defensive_contribution": 0,
        },
    ]
    return pd.DataFrame([*rows, rows[1].copy()])


def test_history_builder_removes_exact_duplicates_and_rejects_conflicts():
    history, removed = build_player_fixture_history(_players(), _gameweeks())

    assert removed == 1
    assert len(history) == 3
    assert history.loc[history["player_code"] == 1001, "position"].unique() == [
        "FWD"
    ]
    assert history.loc[history["player_code"] == 1001, "team"].tolist() == [
        "AAA",
        "BBB",
    ]

    conflict = _gameweeks()
    conflict.loc[3, "minutes"] = 79
    with pytest.raises(ValueError, match="conflicting player-fixture duplicates"):
        build_player_fixture_history(_players(), conflict)


def test_history_builder_reconciles_gameweek_and_season_totals():
    players = _players()
    players.loc[0, "expected_goals"] = 9.9

    with pytest.raises(ValueError, match="totals do not match players"):
        build_player_fixture_history(players, _gameweeks())


def test_import_and_rate_windows_are_content_addressed_and_auditable(tmp_path):
    players_path = tmp_path / "players_raw.csv"
    gameweeks_path = tmp_path / "merged_gw.csv"
    _players().to_csv(players_path, index=False)
    _gameweeks().to_csv(gameweeks_path, index=False)
    database_path = tmp_path / "fpl.duckdb"
    timestamp = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

    first = import_player_fixture_history(
        players_path,
        gameweeks_path,
        season="2025-26",
        source_revision="abc123",
        source_committed_at=timestamp,
        imported_at=timestamp,
        database_path=database_path,
    )
    second = import_player_fixture_history(
        players_path,
        gameweeks_path,
        season="2025-26",
        source_revision="abc123",
        source_committed_at=timestamp,
        imported_at=timestamp,
        database_path=database_path,
    )
    rates = materialize_preseason_rate_history(
        source_import_run_id=first.import_run_id,
        database_path=database_path,
    )
    repeated_rates = materialize_preseason_rate_history(
        source_import_run_id=first.import_run_id,
        database_path=database_path,
    )

    assert first == second
    assert first.player_rows == 2
    assert first.fixture_rows == 3
    assert first.exact_duplicate_rows_removed == 1
    assert rates == repeated_rates
    assert rates.player_rows == 2
    with duckdb.connect(str(database_path), read_only=True) as connection:
        forward = connection.execute(
            """
            SELECT season_minutes, season_starts,
                   long_form_minutes, long_form_expected_goals,
                   short_form_minutes, short_form_expected_goals,
                   long_form_defcon_minutes,
                   short_form_defcon_minutes,
                   data_quality_flags
            FROM player_rate_history
            WHERE rate_run_id = ? AND player_code = 1001
            """,
            [rates.rate_run_id],
        ).fetchone()
        keeper_flags = connection.execute(
            """
            SELECT data_quality_flags FROM player_rate_history
            WHERE rate_run_id = ? AND player_code = 2002
            """,
            [rates.rate_run_id],
        ).fetchone()[0]

    assert forward[:8] == (170, 2, 170, 1.5, 80, 0.5, 170, 170)
    assert forward[8] == "[]"
    assert "ZERO_DEFCON_HISTORY_MINUTES" not in keeper_flags


def test_rate_windows_reject_non_preseason_window_order(tmp_path):
    with pytest.raises(ValueError, match="1 <= short <= long <= 38"):
        materialize_preseason_rate_history(
            source_import_run_id="missing",
            short_form_gameweeks=7,
            long_form_gameweeks=6,
            database_path=tmp_path / "fpl.duckdb",
        )
