from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from fpl_model.validation.release_drift import compare_web_releases
from fpl_model.webapp.service import CurrentSquadSetup, load_web_bootstrap, recommend_web_lineups


def _database(path: Path) -> tuple[int, ...]:
    connection = duckdb.connect(str(path))
    connection.execute(
        """
        CREATE TABLE model_run (
            target_gameweek INTEGER,
            model_run_id VARCHAR,
            source_ingestion_run_id VARCHAR,
            model_version VARCHAR,
            as_of TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            status VARCHAR
        );
        CREATE TABLE player_snapshot (
            ingestion_run_id VARCHAR,
            fpl_id INTEGER,
            player_code BIGINT,
            web_name VARCHAR,
            team_id INTEGER,
            fpl_position VARCHAR,
            price DECIMAL(5,1),
            fpl_status VARCHAR
        );
        CREATE TABLE team_snapshot (
            ingestion_run_id VARCHAR,
            team_id INTEGER,
            short_name VARCHAR
        );
        CREATE TABLE player_fixture_projection (
            model_run_id VARCHAR,
            player_code BIGINT,
            fixture_id INTEGER,
            final_xpts DOUBLE,
            uncertainty DOUBLE,
            data_quality_flags VARCHAR,
            start_probability DOUBLE,
            substitute_appearance_probability DOUBLE,
            opponent_team_id INTEGER,
            is_home BOOLEAN
        );
        """
    )
    source_id = "snapshot_web_test"
    as_of = datetime(2026, 8, 25, 8, tzinfo=UTC)
    for gameweek in (2, 3, 4):
        connection.execute(
            "INSERT INTO model_run VALUES (?, ?, ?, ?, ?, ?, 'completed')",
            [
                gameweek,
                f"run_gw{gameweek}",
                source_id,
                "web_test_v1",
                as_of,
                as_of + timedelta(minutes=gameweek),
            ],
        )
    for team_id in range(1, 7):
        connection.execute(
            "INSERT INTO team_snapshot VALUES (?, ?, ?)",
            [source_id, team_id, f"T{team_id}"],
        )

    positions = ("GK", "GK", *("DEF",) * 5, *("MID",) * 5, *("FWD",) * 3)
    fpl_ids = tuple(range(1, 16))
    for fpl_id, position in zip(fpl_ids, positions, strict=True):
        team_id = ((fpl_id - 1) % 6) + 1
        opponent_team_id = (team_id % 6) + 1  # a distinct team in the same 6-team pool
        connection.execute(
            "INSERT INTO player_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, 'a')",
            [source_id, fpl_id, 10_000 + fpl_id, f"Player {fpl_id}", team_id, position, 5.0],
        )
        for gameweek in (2, 3, 4):
            connection.execute(
                "INSERT INTO player_fixture_projection VALUES "
                "(?, ?, ?, ?, NULL, '[]', 0.9, 0.05, ?, ?)",
                [
                    f"run_gw{gameweek}",
                    10_000 + fpl_id,
                    gameweek * 100 + fpl_id,
                    fpl_id / 2,
                    opponent_team_id,
                    fpl_id % 2 == 0,
                ],
            )
    connection.close()
    return fpl_ids


def test_web_bootstrap_and_lineups_use_latest_compatible_horizon(tmp_path: Path):
    database_path = tmp_path / "web.duckdb"
    fpl_ids = _database(database_path)

    bootstrap = load_web_bootstrap(database_path)
    result = recommend_web_lineups(fpl_ids, database_path=database_path)

    assert bootstrap["release"]["health"] == "research"
    assert [row["gameweek"] for row in bootstrap["release"]["model_runs"]] == [2, 3, 4]
    assert len(bootstrap["players"]) == 15
    assert result["horizon"] == [2, 3, 4]
    assert len(result["lineups"]) == 3
    assert all(row["formation"] == "3-4-3" for row in result["lineups"])
    assert all(len(row["starters"]) == 11 for row in result["lineups"])
    assert result["squad_rating"]["schema_version"] == "squad_rating_v1"
    assert result["squad_rating"]["input"]["raw_cumulative_xpts"] == pytest.approx(
        result["cumulative_xpts"]
    )
    assert result["squad_rating"]["input"]["squad_fpl_ids"] == sorted(fpl_ids)
    assert len(result["squad_rating"]["input"]["optimized_decisions"]) == 3
    # This deliberately tiny fixture has exactly one legal squad, below the
    # production contract's minimum benchmark population.
    assert result["squad_rating"]["available"] is False
    assert result["squad_rating"]["model_strength"] is None


def test_web_lineup_rejects_duplicate_squad_players(tmp_path: Path):
    database_path = tmp_path / "web.duckdb"
    fpl_ids = _database(database_path)

    with pytest.raises(ValueError, match="15 unique players"):
        recommend_web_lineups((*fpl_ids[:-1], fpl_ids[0]), database_path=database_path)


def test_weekly_lineup_reports_marginal_xpts_against_loaded_current_setup(tmp_path: Path):
    release_path = tmp_path / "release.json"
    fpl_ids = _release_file(release_path)
    setup = CurrentSquadSetup(
        gameweek=2,
        starter_fpl_ids=(1, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14),
        bench_fpl_ids=(2, 6, 7, 15),
        captain_fpl_id=9,
        vice_captain_fpl_id=5,
    )

    result = recommend_web_lineups(fpl_ids, release_path=release_path, current_setup=setup)

    comparison = result["lineups"][0]["current_setup_comparison"]
    assert comparison["basis"] == "loaded_fpl_picks"
    assert comparison["current_formation"] == "3-5-2"
    assert comparison["current_total_xpts"] == pytest.approx(49.5)
    assert comparison["recommended_total_xpts"] == pytest.approx(59.5)
    assert comparison["marginal_xpts"] == pytest.approx(10.0)
    assert {row["fpl_id"] for row in comparison["started"]} == {2, 6, 7, 15}
    assert {row["fpl_id"] for row in comparison["benched"]} == {1, 3, 4, 8}
    assert comparison["captain_change"]["from"]["fpl_id"] == 9
    assert comparison["captain_change"]["to"]["fpl_id"] == 15
    assert comparison["bench_order_changed"] is True
    assert result["lineups"][1]["current_setup_comparison"] is None


def _release_file(path: Path, *, player_one_xpts: float = 0.5) -> tuple[int, ...]:
    positions = ("GK", "GK", *("DEF",) * 5, *("MID",) * 5, *("FWD",) * 3)
    players = []
    for fpl_id, position in enumerate(positions, start=1):
        xpts = player_one_xpts if fpl_id == 1 else fpl_id / 2
        players.append(
            {
                "fpl_id": fpl_id,
                "player_code": 10_000 + fpl_id,
                "name": f"Player {fpl_id}",
                "team_id": ((fpl_id - 1) % 6) + 1,
                "team": f"T{((fpl_id - 1) % 6) + 1}",
                "position": position,
                "price_tenths": 50,
                "status": "a",
                "gameweeks": {
                    str(gameweek): {
                        "xpts": xpts,
                        "appearance_probability": 0.95,
                        "uncertainty": None,
                        "quality_flags": [],
                    }
                    for gameweek in (2, 3, 4)
                },
            }
        )
    payload = {
        "schema_version": "fpl_web_release_v1",
        "release": {
            "health": "shadow",
            "source_ingestion_run_id": path.stem,
            "model_version": "web_test_v1",
            "planning_as_of": "2026-08-26T08:00:00+00:00",
            "model_runs": [
                {"gameweek": gameweek, "model_run_id": f"run_gw{gameweek}"}
                for gameweek in (2, 3, 4)
            ],
        },
        "players": players,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return tuple(range(1, 16))


def test_compact_release_runs_without_database_and_reports_drift(tmp_path: Path):
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    fpl_ids = _release_file(before_path)
    _release_file(after_path, player_one_xpts=3.0)

    bootstrap = load_web_bootstrap(
        tmp_path / "missing.duckdb",
        release_path=before_path,
    )
    lineups = recommend_web_lineups(fpl_ids, release_path=before_path)
    drift = compare_web_releases(before_path=before_path, after_path=after_path)

    assert bootstrap["release"]["health"] == "shadow"
    assert lineups["health"] == "shadow"
    assert len(lineups["lineups"]) == 3
    assert drift.material_change
    assert drift.report["players"]["material_change_count"] == 3
