from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pytest

from fpl_model.decision.rolling import plan_three_gameweeks
from fpl_model.decision.rolling_store import load_rolling_inputs
from tests.test_lineup_store import _model_run
from tests.test_squad_snapshot import _import

AS_OF = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)


def _seed_horizon(tmp_path):
    imported = _import(
        tmp_path,
        free_transfers=1,
        unlimited_transfers=False,
    )
    database_path = tmp_path / "fpl.duckdb"
    first_model = _model_run(database_path)
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
        connection.execute(
            """
            INSERT INTO player_fixture_projection VALUES (
                ?, 1016, 6101, 1, 2, FALSE, 0.8, 0.1, 70.0,
                1.0, 0.0, 1.0, 1.0, ''
            )
            """,
            [first_model],
        )
        for gameweek, model_run_id, deadline in (
            (2, "model_gw2", datetime(2026, 8, 28, 17, 30, tzinfo=UTC)),
            (3, "model_gw3", datetime(2026, 9, 4, 17, 30, tzinfo=UTC)),
        ):
            connection.execute(
                """
                INSERT INTO model_run (
                    model_run_id, target_gameweek, as_of, deadline, model_version,
                    source_ingestion_run_id, status, completed_at
                ) VALUES (?, ?, ?, ?, 'test', 'official', 'completed', ?)
                """,
                [model_run_id, gameweek, AS_OF, deadline, AS_OF],
            )
            connection.executemany(
                "INSERT INTO player_fixture_projection VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        model_run_id,
                        1000 + fpl_id,
                        gameweek * 1000 + fpl_id,
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
                        "",
                    )
                    for fpl_id in range(1, 17)
                ],
            )
    return imported, database_path, {1: first_model, 2: "model_gw2", 3: "model_gw3"}


def test_loads_three_frozen_model_runs_and_builds_a_plan(tmp_path):
    imported, database_path, model_runs = _seed_horizon(tmp_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        inputs = load_rolling_inputs(
            connection,
            squad_snapshot_id=imported.squad_snapshot_id,
            model_run_ids=model_runs,
        )
    result = plan_three_gameweeks(inputs.lineup_inputs.squad, inputs.pools)

    assert inputs.planning_as_of == AS_OF
    assert inputs.model_version == "test"
    assert [pool.gameweek for pool in inputs.pools] == [1, 2, 3]
    assert [row.projected_players for row in inputs.diagnostics] == [16, 16, 16]
    assert all(len(step.lineup.starters) == 11 for step in result.recommended.steps)


def test_rejects_future_run_created_from_a_different_as_of_or_model_version(tmp_path):
    imported, database_path, model_runs = _seed_horizon(tmp_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            "UPDATE model_run SET as_of = ? WHERE model_run_id = 'model_gw3'",
            [datetime(2026, 8, 21, 10, 0, tzinfo=UTC)],
        )
    with duckdb.connect(str(database_path), read_only=True) as connection:
        with pytest.raises(ValueError, match="one frozen as_of"):
            load_rolling_inputs(
                connection,
                squad_snapshot_id=imported.squad_snapshot_id,
                model_run_ids=model_runs,
            )

    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            "UPDATE model_run SET as_of = ?, model_version = 'changed' WHERE model_run_id = 'model_gw3'",
            [AS_OF],
        )
    with duckdb.connect(str(database_path), read_only=True) as connection:
        with pytest.raises(ValueError, match="one model_version"):
            load_rolling_inputs(
                connection,
                squad_snapshot_id=imported.squad_snapshot_id,
                model_run_ids=model_runs,
            )


def test_rejects_missing_or_nonconsecutive_model_run_contract(tmp_path):
    imported, database_path, model_runs = _seed_horizon(tmp_path)
    with duckdb.connect(str(database_path), read_only=True) as connection:
        with pytest.raises(ValueError, match="exactly three"):
            load_rolling_inputs(
                connection,
                squad_snapshot_id=imported.squad_snapshot_id,
                model_run_ids={1: model_runs[1], 2: model_runs[2]},
            )
        with pytest.raises(ValueError, match="must be consecutive"):
            load_rolling_inputs(
                connection,
                squad_snapshot_id=imported.squad_snapshot_id,
                model_run_ids={1: model_runs[1], 2: model_runs[2], 4: model_runs[3]},
            )


def test_rejects_horizon_run_completed_after_the_first_deadline(tmp_path):
    imported, database_path, model_runs = _seed_horizon(tmp_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            "UPDATE model_run SET completed_at = ? WHERE model_run_id = 'model_gw3'",
            [datetime(2026, 8, 22, 10, 0, tzinfo=UTC)],
        )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        with pytest.raises(ValueError, match="completed by the first deadline"):
            load_rolling_inputs(
                connection,
                squad_snapshot_id=imported.squad_snapshot_id,
                model_run_ids=model_runs,
            )
