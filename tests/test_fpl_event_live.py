from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pytest

from fpl_model.ingest.fpl_event_live import persist_fpl_event_live
from fpl_model.storage import initialize_database


def _seed_source(database_path, *, final: bool) -> None:
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('snapshot', 'official_fpl_api',
                      '2026-08-24T09:00:00+07:00', 'completed');
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES ('snapshot', '2026-27', 1, 1001, 'A', 'Player',
                      'Player', 1, 'MID', 7.5, 'a');
            INSERT INTO gameweek_snapshot VALUES (
                'snapshot', 1, 'Gameweek 1', '2026-08-22T00:30:00+07:00',
                NULL, ?, ?, FALSE, TRUE, FALSE
            );
            """,
            [final, final],
        )


def _payload() -> dict[str, object]:
    return {
        "elements": [
            {
                "id": 1,
                "modified": False,
                "stats": {
                    "played": True,
                    "minutes": 90,
                    "starts": 1,
                    "goals_scored": 1,
                    "assists": 0,
                    "saves": 0,
                    "yellow_cards": 0,
                    "red_cards": 0,
                    "bonus": 3,
                    "bps": 40,
                    "defensive_contribution": 4,
                    "expected_goals": "0.55",
                    "expected_assists": "0.10",
                    "expected_goals_conceded": "0.80",
                    "total_points": 10,
                },
                "explain": [],
            }
        ]
    }


def test_event_live_requires_final_checked_gameweek_by_default(tmp_path):
    database_path = tmp_path / "model.duckdb"
    _seed_source(database_path, final=False)

    with pytest.raises(ValueError, match="is not final"):
        persist_fpl_event_live(
            payload=_payload(),
            source_ingestion_run_id="snapshot",
            gameweek=1,
            captured_at=datetime(2026, 8, 24, 3, 0, tzinfo=UTC),
            season="2026-27",
            database_path=database_path,
            raw_root=tmp_path / "raw",
        )

    assert not (tmp_path / "raw").exists()


def test_event_live_is_immutable_idempotent_and_keeps_lineage(tmp_path):
    database_path = tmp_path / "model.duckdb"
    _seed_source(database_path, final=True)
    kwargs = {
        "payload": _payload(),
        "source_ingestion_run_id": "snapshot",
        "gameweek": 1,
        "captured_at": datetime(2026, 8, 24, 3, 0, tzinfo=UTC),
        "season": "2026-27",
        "database_path": database_path,
        "raw_root": tmp_path / "raw",
    }

    first = persist_fpl_event_live(**kwargs)
    second = persist_fpl_event_live(**kwargs)

    assert second == first
    assert first.status == "completed"
    assert first.source_path.is_file()
    with duckdb.connect(str(database_path), read_only=True) as connection:
        run = connection.execute(
            """
            SELECT source_ingestion_run_id, gameweek, event_finished,
                   data_checked, player_rows, status
            FROM fpl_event_live_run
            """
        ).fetchone()
        stat = connection.execute(
            """
            SELECT player_code, minutes, starts, expected_goals, total_points
            FROM player_gameweek_stat
            """
        ).fetchone()

    assert run == ("snapshot", 1, True, True, 1, "completed")
    assert stat[:3] == (1001, 90, 1)
    assert stat[3] == pytest.approx(0.55)
    assert stat[4] == 10
