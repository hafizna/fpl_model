from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pytest

from fpl_model.decision.lineup import recommend_lineup
from fpl_model.decision.lineup_store import load_lineup_inputs
from fpl_model.decision.transfer_store import load_transfer_inputs
from tests.test_squad_snapshot import _import


def _model_run(database_path, *, missing_fpl_id: int | None = None) -> str:
    model_run_id = "model_gw1"
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO model_run (
                model_run_id, target_gameweek, as_of, deadline, model_version,
                source_ingestion_run_id, status, completed_at
            ) VALUES (?, 1, ?, ?, 'test', 'official', 'completed', ?)
            """,
            [
                model_run_id,
                datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
                datetime(2026, 8, 21, 17, 30, tzinfo=UTC),
                datetime(2026, 8, 20, 10, 5, tzinfo=UTC),
            ],
        )
        rows = []
        for fpl_id in range(1, 16):
            if fpl_id == missing_fpl_id:
                continue
            rows.append(
                (
                    model_run_id,
                    1000 + fpl_id,
                    5000 + fpl_id,
                    ((fpl_id - 1) % 5) + 1,
                    (fpl_id % 5) + 1,
                    True,
                    0.8,
                    0.1,
                    70.0,
                    float(fpl_id),
                    0.0,
                    float(fpl_id),
                    1.0,
                    "LOW_RATE_COVERAGE" if fpl_id == 1 else "",
                )
            )
        connection.executemany(
            "INSERT INTO player_fixture_projection VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        # A second fixture proves that the store aggregates DGW rows before
        # the lineup search rather than choosing one fixture arbitrarily.
        if missing_fpl_id != 15:
            connection.execute(
                """
                INSERT INTO player_fixture_projection VALUES (
                    ?, 1015, 9000, 5, 1, FALSE, 0.8, 0.1, 70.0,
                    5.0, 0.0, 5.0, 2.0, 'DOUBLE_GAMEWEEK'
                )
                """,
                [model_run_id],
            )
    return model_run_id


def test_loads_same_gameweek_squad_and_aggregates_fixture_projections(tmp_path):
    imported = _import(tmp_path)
    database_path = tmp_path / "fpl.duckdb"
    model_run_id = _model_run(database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        inputs = load_lineup_inputs(
            connection,
            squad_snapshot_id=imported.squad_snapshot_id,
            model_run_id=model_run_id,
        )
    projection_15 = next(row for row in inputs.projections if row.fpl_id == 15)
    recommendation = recommend_lineup(inputs.squad, inputs.projections)

    assert projection_15.expected_points == pytest.approx(20.0)
    assert projection_15.uncertainty == pytest.approx(5**0.5)
    assert projection_15.data_quality_flags == ("DOUBLE_GAMEWEEK",)
    assert recommendation.captain.fpl_id == 15


def test_rejects_model_run_with_missing_squad_projection(tmp_path):
    imported = _import(tmp_path)
    database_path = tmp_path / "fpl.duckdb"
    model_run_id = _model_run(database_path, missing_fpl_id=7)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        with pytest.raises(ValueError, match="missing projections.*7"):
            load_lineup_inputs(
                connection,
                squad_snapshot_id=imported.squad_snapshot_id,
                model_run_id=model_run_id,
            )


def test_loads_non_owned_transfer_targets_and_aggregates_their_dgw(tmp_path):
    imported = _import(tmp_path)
    database_path = tmp_path / "fpl.duckdb"
    model_run_id = _model_run(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES ('official', '2026-27', 16, 1016, 'Test', 'Player 16',
                      'Player 16', 1, 'FWD', 5.0, 'a')
            """
        )
        connection.execute(
            """
            INSERT INTO player_status_snapshot VALUES (
                'official', 16, TRUE, TRUE, FALSE, 0.0, 0, 0, 0, 0,
                0, 0, 0.0, NULL, NULL, NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO player_fixture_projection VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (model_run_id, 1016, 6001, 1, 2, False, 0.8, 0.1, 70.0, 4.0, 0.0, 4.0, 1.0, ""),
                (
                    model_run_id,
                    1016,
                    6002,
                    1,
                    3,
                    True,
                    0.8,
                    0.1,
                    70.0,
                    3.0,
                    0.0,
                    3.0,
                    2.0,
                    "DOUBLE_GAMEWEEK",
                ),
            ],
        )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        inputs, targets, excluded, excluded_unavailable = load_transfer_inputs(
            connection,
            squad_snapshot_id=imported.squad_snapshot_id,
            model_run_id=model_run_id,
        )

    assert inputs.source_ingestion_run_id == "official"
    assert excluded == 0
    assert excluded_unavailable == 0
    assert len(targets) == 1
    assert targets[0].projection.expected_points == pytest.approx(7.0)
    assert targets[0].projection.uncertainty == pytest.approx(5**0.5)
    assert targets[0].projection.data_quality_flags == ("DOUBLE_GAMEWEEK",)
