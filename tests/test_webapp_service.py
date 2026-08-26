from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from fpl_model.webapp.service import load_web_bootstrap, recommend_web_lineups


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
            substitute_appearance_probability DOUBLE
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
        connection.execute(
            "INSERT INTO player_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, 'a')",
            [source_id, fpl_id, 10_000 + fpl_id, f"Player {fpl_id}", team_id, position, 5.0],
        )
        for gameweek in (2, 3, 4):
            connection.execute(
                "INSERT INTO player_fixture_projection VALUES (?, ?, ?, ?, NULL, '[]', 0.9, 0.05)",
                [f"run_gw{gameweek}", 10_000 + fpl_id, gameweek * 100 + fpl_id, fpl_id / 2],
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


def test_web_lineup_rejects_duplicate_squad_players(tmp_path: Path):
    database_path = tmp_path / "web.duckdb"
    fpl_ids = _database(database_path)

    with pytest.raises(ValueError, match="15 unique players"):
        recommend_web_lineups((*fpl_ids[:-1], fpl_ids[0]), database_path=database_path)
