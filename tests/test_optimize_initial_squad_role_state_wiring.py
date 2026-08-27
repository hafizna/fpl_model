"""Integration coverage: optimize_initial_squad.py's role_state wiring is correct.

Mirrors test_role_state_wiring.py's approach: exercises the exact per-Gameweek
role state lookup construction scripts/optimize_initial_squad.py uses, against
real store outputs plus a seeded availability/appearance lineage for all three
Gameweeks in the horizon. Part of this session's P0 sign-off gap closure:
optimize_initial_squad.py previously carried `transparency` but not
`role_state`.
"""

from __future__ import annotations

import duckdb

from fpl_model.decision.initial_squad_store import load_initial_squad_inputs
from fpl_model.validation.role_state import LIKELY_STARTER, UNAVAILABLE, load_role_states
from tests.test_role_state_wiring import _add_role_state_lineage
from tests.test_rolling_store import _seed_horizon


def test_optimize_initial_squad_wiring_attaches_role_state_per_gameweek(tmp_path):
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
    for gameweek, model_run_id in model_runs.items():
        _add_role_state_lineage(
            database_path,
            model_run_id=model_run_id,
            source_ingestion_run_id="official",
            gameweek=gameweek,
        )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        inputs = load_initial_squad_inputs(connection, model_run_ids=model_runs)
        # Mirrors scripts/optimize_initial_squad.py's role_state wiring exactly.
        role_state_by_gameweek = {
            gameweek: load_role_states(
                connection,
                model_run_id=run_id,
                fpl_ids=tuple(target.player.fpl_id for target in pool.players),
            )
            for (gameweek, run_id), pool in zip(inputs.model_run_ids, inputs.pools, strict=True)
        }

    for gameweek in (1, 2, 3):
        by_id = role_state_by_gameweek[gameweek]
        assert by_id[1].role_state == LIKELY_STARTER
        assert by_id[2].role_state == UNAVAILABLE
