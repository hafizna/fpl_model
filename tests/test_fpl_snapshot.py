from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pytest

from fpl_model.ingest.fpl_snapshot import persist_fpl_snapshot


def _snapshot_payload() -> tuple[dict[str, object], list[dict[str, object]]]:
    player = {
        "id": 101,
        "code": 9001,
        "first_name": "Example",
        "second_name": "Player",
        "web_name": "Player",
        "team": 1,
        "element_type": 3,
        "now_cost": 75,
        "status": "d",
        "chance_of_playing_this_round": 50,
        "chance_of_playing_next_round": 75,
        "news": "Knock - 75% chance of playing",
        "news_added": "2026-08-17T08:00:00Z",
        "can_select": True,
        "can_transact": True,
        "removed": False,
        "selected_by_percent": "12.3",
        "transfers_in": 100,
        "transfers_in_event": 10,
        "transfers_out": 20,
        "transfers_out_event": 2,
        "event_points": 0,
        "total_points": 0,
        "form": "0.0",
        "ep_this": "2.1",
        "ep_next": None,
        "team_join_date": "2026-07-01",
        "minutes": 0,
        "starts": 0,
        "goals_scored": 0,
        "assists": 0,
        "clean_sheets": 0,
        "goals_conceded": 0,
        "saves": 0,
        "yellow_cards": 0,
        "red_cards": 0,
        "bonus": 0,
        "bps": 0,
        "own_goals": 0,
        "penalties_saved": 0,
        "penalties_missed": 0,
        "defensive_contribution": 0,
        "clearances_blocks_interceptions": 0,
        "recoveries": 0,
        "tackles": 0,
        "expected_goals": "0.0",
        "expected_assists": "0.0",
        "expected_goal_involvements": "0.0",
        "expected_goals_conceded": "0.0",
    }
    teams = [
        {
            "id": team_id,
            "code": 100 + team_id,
            "name": name,
            "short_name": name[:3].upper(),
            "unavailable": False,
            "strength": 3,
            "strength_overall_home": 1100,
            "strength_overall_away": 1090,
            "strength_attack_home": 1080,
            "strength_attack_away": 1070,
            "strength_defence_home": 1060,
            "strength_defence_away": 1050,
        }
        for team_id, name in ((1, "Alpha"), (2, "Beta"))
    ]
    bootstrap: dict[str, object] = {
        "elements": [player],
        "teams": teams,
        "events": [
            {
                "id": 1,
                "name": "Gameweek 1",
                "deadline_time": "2026-08-15T10:00:00Z",
                "release_time": None,
                "finished": False,
                "data_checked": False,
                "is_previous": False,
                "is_current": False,
                "is_next": True,
            }
        ],
    }
    fixtures = [
        {
            "id": 501,
            "event": 1,
            "kickoff_time": "2026-08-15T12:30:00Z",
            "team_h": 1,
            "team_a": 2,
            "started": False,
            "finished": False,
        }
    ]
    return bootstrap, fixtures


def test_persists_complete_fpl_snapshot_and_raw_provenance(tmp_path):
    bootstrap, fixtures = _snapshot_payload()
    database_path = tmp_path / "processed" / "fpl.duckdb"
    raw_root = tmp_path / "raw"
    captured_at = datetime(2026, 8, 17, 9, 30, tzinfo=UTC)

    result = persist_fpl_snapshot(
        bootstrap=bootstrap,
        fixtures=fixtures,
        captured_at=captured_at,
        season="2026-27",
        database_path=database_path,
        raw_root=raw_root,
    )

    assert result.players == 1
    assert result.teams == 2
    assert result.gameweeks == 1
    assert result.fixtures == 1
    assert result.manifest_path.exists()
    assert (result.manifest_path.parent / "bootstrap-static.json").exists()
    assert (result.manifest_path.parent / "fixtures.json").exists()

    with duckdb.connect(str(database_path), read_only=True) as connection:
        player = connection.execute(
            """
            SELECT season, player_code, fpl_status, chance_of_playing_next_round, news
            FROM player_snapshot
            """
        ).fetchone()
        status = connection.execute(
            "SELECT selected_by_percent, expected_points_next FROM player_status_snapshot"
        ).fetchone()
        deadline = connection.execute(
            "SELECT deadline_time FROM gameweek_snapshot WHERE gameweek = 1"
        ).fetchone()[0]
        stats = connection.execute(
            "SELECT minutes, defensive_contribution FROM player_season_stat_snapshot"
        ).fetchone()

    assert player == (
        "2026-27",
        9001,
        "d",
        75,
        "Knock - 75% chance of playing",
    )
    assert status[0] == pytest.approx(12.3)
    assert status[1] is None
    assert deadline == datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
    assert stats == (0, 0)


def test_repeating_identical_snapshot_is_idempotent(tmp_path):
    bootstrap, fixtures = _snapshot_payload()
    database_path = tmp_path / "fpl.duckdb"
    arguments = {
        "bootstrap": bootstrap,
        "fixtures": fixtures,
        "captured_at": datetime(2026, 8, 17, 9, 30, tzinfo=UTC),
        "season": "2026-27",
        "database_path": database_path,
        "raw_root": tmp_path / "raw",
    }

    first = persist_fpl_snapshot(**arguments)
    second = persist_fpl_snapshot(**arguments)

    assert first.ingestion_run_id == second.ingestion_run_id
    with duckdb.connect(str(database_path), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM ingestion_run").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM player_snapshot").fetchone()[0] == 1


def test_finished_provisional_fixture_is_stored_as_analytically_finished(tmp_path):
    bootstrap, fixtures = _snapshot_payload()
    fixtures[0]["finished_provisional"] = True
    database_path = tmp_path / "fpl.duckdb"

    persist_fpl_snapshot(
        bootstrap=bootstrap,
        fixtures=fixtures,
        captured_at=datetime(2026, 9, 1, 7, 0, tzinfo=UTC),
        season="2026-27",
        database_path=database_path,
        raw_root=tmp_path / "raw",
    )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        fixture_finished = connection.execute(
            "SELECT finished FROM fixture_snapshot WHERE fixture_id = 501"
        ).fetchone()[0]
        gameweek_finished = connection.execute(
            "SELECT finished FROM gameweek_snapshot WHERE gameweek = 1"
        ).fetchone()[0]

    assert fixture_finished is True
    assert gameweek_finished is False


def test_invalid_model_input_rolls_back_all_database_rows(tmp_path):
    bootstrap, fixtures = _snapshot_payload()
    bootstrap["elements"][0]["minutes"] = "not-a-number"  # type: ignore[index]
    database_path = tmp_path / "fpl.duckdb"

    with pytest.raises(ValueError, match="elements.minutes"):
        persist_fpl_snapshot(
            bootstrap=bootstrap,
            fixtures=fixtures,
            captured_at=datetime(2026, 8, 17, 9, 30, tzinfo=UTC),
            season="2026-27",
            database_path=database_path,
            raw_root=tmp_path / "raw",
        )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM ingestion_run").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM player_snapshot").fetchone()[0] == 0
    assert not (tmp_path / "raw").exists()


def test_preseason_null_team_strength_is_preserved(tmp_path):
    bootstrap, fixtures = _snapshot_payload()
    bootstrap["teams"][0]["strength"] = None  # type: ignore[index]
    database_path = tmp_path / "fpl.duckdb"

    persist_fpl_snapshot(
        bootstrap=bootstrap,
        fixtures=fixtures,
        captured_at=datetime(2026, 8, 17, 9, 30, tzinfo=UTC),
        season="2026-27",
        database_path=database_path,
        raw_root=tmp_path / "raw",
    )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        strength = connection.execute(
            "SELECT strength FROM team_snapshot WHERE team_id = 1"
        ).fetchone()[0]
    assert strength is None
