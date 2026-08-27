from __future__ import annotations

import duckdb
import pytest

from fpl_model.storage import initialize_database
from fpl_model.validation.material_conflict import (
    UNEXPECTED_BLANK,
    UNEXPECTED_SUBSTANTIAL_START,
    audit_material_conflicts,
    material_conflict_report,
)


def _seed(database_path, *, event_finished=True, data_checked=True, live_gameweek=2) -> None:
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('snapshot', 'official_fpl_api', '2026-08-20T00:00:00+00:00', 'completed');

            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES
                ('snapshot', '2026-27', 1, 1001, 'A', 'Unexpected', 'Unexpected', 1, 'MID', 8.0, 'a'),
                ('snapshot', '2026-27', 2, 1002, 'B', 'Blank', 'Blank', 1, 'FWD', 9.0, 'a'),
                ('snapshot', '2026-27', 3, 1003, 'C', 'AsExpected', 'AsExpected', 1, 'DEF', 5.0, 'a');

            INSERT INTO model_run (
                model_run_id, target_gameweek, as_of, deadline, model_version,
                source_ingestion_run_id, status, completed_at
            ) VALUES (
                'model', 2, '2026-08-20T00:00:00+00:00', '2026-08-29T00:00:00+00:00',
                'test', 'snapshot', 'completed', current_timestamp
            );

            -- player 1: projected as a near-certain bench player, but actually
            -- started and played 90 minutes -> UNEXPECTED_SUBSTANTIAL_START.
            -- player 2: projected as a near-certain starter, but actually
            -- blanked with 0 minutes -> UNEXPECTED_BLANK.
            -- player 3: projected and outcome agree -> no conflict.
            INSERT INTO player_fixture_projection VALUES
                ('model', 1001, 101, 1, 2, TRUE, 0.05, 0.05, 10.0, 1.0, 0.0, 1.0, NULL, '[]'),
                ('model', 1002, 102, 1, 2, TRUE, 0.9, 0.05, 82.0, 6.0, 0.0, 6.0, NULL, '[]'),
                ('model', 1003, 103, 1, 2, TRUE, 0.9, 0.05, 82.0, 4.0, 0.0, 4.0, NULL, '[]');
            """
        )
        connection.execute(
            """
            INSERT INTO fpl_event_live_run VALUES (
                'live', 'snapshot', '2026-27', ?, '2026-08-30T00:00:00+00:00',
                'event.json', 'sha', ?, ?, 3, ?, current_timestamp
            )
            """,
            [
                live_gameweek,
                event_finished,
                data_checked,
                "completed" if (event_finished and data_checked) else "provisional",
            ],
        )
        connection.execute(
            """
            INSERT INTO player_gameweek_stat VALUES
                ('live', 1, 1001, TRUE, 90, 1, 1, 0, 0, 0, 0, 5, 40, 0, 0.2, 0.1, 0.4, 6, FALSE, '[]'),
                ('live', 2, 1002, FALSE, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0, FALSE, '[]'),
                ('live', 3, 1003, TRUE, 90, 1, 0, 0, 0, 0, 0, 2, 30, 0, 0.1, 0.0, 0.3, 4, FALSE, '[]')
            """
        )


def test_detects_both_named_conflict_shapes(tmp_path):
    database_path = tmp_path / "conflict.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        conflicts = audit_material_conflicts(
            connection, model_run_id="model", live_run_id="live"
        )

    by_fpl_id = {row.fpl_id: row for row in conflicts}
    assert by_fpl_id[1].conflict_type == UNEXPECTED_SUBSTANTIAL_START
    assert by_fpl_id[2].conflict_type == UNEXPECTED_BLANK
    assert 3 not in by_fpl_id
    assert len(conflicts) == 2


def test_conflicts_are_sorted_by_fpl_id(tmp_path):
    database_path = tmp_path / "conflict.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        conflicts = audit_material_conflicts(
            connection, model_run_id="model", live_run_id="live"
        )

    assert [row.fpl_id for row in conflicts] == sorted(row.fpl_id for row in conflicts)


def test_rejects_a_provisional_not_yet_final_event_live_run(tmp_path):
    database_path = tmp_path / "conflict.duckdb"
    _seed(database_path, event_finished=True, data_checked=False)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        with pytest.raises(ValueError, match="not final"):
            audit_material_conflicts(connection, model_run_id="model", live_run_id="live")


def test_rejects_gameweek_mismatch_between_model_run_and_live_run(tmp_path):
    database_path = tmp_path / "conflict.duckdb"
    _seed(database_path, live_gameweek=3)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        with pytest.raises(ValueError, match="targets GW2 but event-live run is for GW3"):
            audit_material_conflicts(connection, model_run_id="model", live_run_id="live")


def test_raises_on_unknown_model_run_id(tmp_path):
    database_path = tmp_path / "conflict.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        with pytest.raises(ValueError, match="unknown model_run_id"):
            audit_material_conflicts(connection, model_run_id="nope", live_run_id="live")


def test_raises_on_unknown_live_run_id(tmp_path):
    database_path = tmp_path / "conflict.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        with pytest.raises(ValueError, match="unknown live_run_id"):
            audit_material_conflicts(connection, model_run_id="model", live_run_id="nope")


def test_material_conflict_report_serialises(tmp_path):
    database_path = tmp_path / "conflict.duckdb"
    _seed(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        conflicts = audit_material_conflicts(
            connection, model_run_id="model", live_run_id="live"
        )

    report = material_conflict_report(conflicts)
    assert len(report) == 2
    assert all("reason" in row and row["reason"] for row in report)
