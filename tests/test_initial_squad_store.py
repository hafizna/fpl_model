from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pytest

from fpl_model.decision.initial_squad import optimize_initial_squad
from fpl_model.decision.initial_squad_store import load_initial_squad_inputs
from tests.test_rolling_store import _seed_horizon


def test_loads_public_horizon_without_a_manager_snapshot_argument(tmp_path):
    _, database_path, model_runs = _seed_horizon(tmp_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.executemany(
            """
            INSERT INTO player_status_snapshot VALUES (
                'official', ?, TRUE, TRUE, FALSE, 0.0, 0, 0, 0, 0,
                0, 0, 0.0, NULL, NULL, NULL
            )
            """,
            [(fpl_id,) for fpl_id in range(1, 16)],
        )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        inputs = load_initial_squad_inputs(connection, model_run_ids=model_runs)
    result = optimize_initial_squad(
        inputs.pools,
        beam_width=100,
        candidates_per_position_per_lens=20,
    )

    assert inputs.source_ingestion_run_id == "official"
    assert inputs.model_version == "test"
    assert [pool.gameweek for pool in inputs.pools] == [1, 2, 3]
    assert [row.projected_players for row in inputs.diagnostics] == [16, 16, 16]
    assert len(result.recommended.squad.players) == 15


def test_rejects_horizon_run_completed_after_first_deadline(tmp_path):
    _, database_path, model_runs = _seed_horizon(tmp_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            "UPDATE model_run SET completed_at = ? WHERE model_run_id = 'model_gw3'",
            [datetime(2026, 8, 22, 10, 0, tzinfo=UTC)],
        )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        with pytest.raises(ValueError, match="completed by the first deadline"):
            load_initial_squad_inputs(connection, model_run_ids=model_runs)
