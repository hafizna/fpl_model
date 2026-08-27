"""Integration coverage: plan_three_gameweeks.py's role_state wiring is correct.

Mirrors test_role_state_wiring.py's approach: exercises the exact per-Gameweek
role state lookup construction scripts/plan_three_gameweeks.py uses
(load_role_states keyed by each Gameweek's own model_run_id), against real
store outputs plus a seeded availability/appearance lineage for all three
Gameweeks in the horizon. Part of this session's P0 sign-off gap closure:
plan_three_gameweeks.py previously carried `transparency` but not
`role_state`, so a rolling transfer plan involving a ROTATION-state player
carried no visible warning.
"""

from __future__ import annotations

import duckdb

from fpl_model.decision.rolling_store import load_rolling_inputs
from fpl_model.validation.role_state import LIKELY_STARTER, UNAVAILABLE, load_role_states
from tests.test_role_state_wiring import _add_role_state_lineage
from tests.test_rolling_store import _seed_horizon


def test_plan_three_gameweeks_wiring_attaches_role_state_per_gameweek(tmp_path):
    imported, database_path, model_runs = _seed_horizon(tmp_path)
    for gameweek, model_run_id in model_runs.items():
        _add_role_state_lineage(
            database_path,
            model_run_id=model_run_id,
            source_ingestion_run_id="official",
            gameweek=gameweek,
        )

    with duckdb.connect(str(database_path)) as connection:
        inputs = load_rolling_inputs(
            connection,
            squad_snapshot_id=imported.squad_snapshot_id,
            model_run_ids=model_runs,
        )
        # Mirrors scripts/plan_three_gameweeks.py's role_state wiring exactly.
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
        # fpl_id=1 resolved eligible with start_probability=0.8 in each
        # Gameweek's own seeded lineage -> LIKELY_STARTER every Gameweek.
        assert by_id[1].role_state == LIKELY_STARTER
        # fpl_id=2 resolved ineligible in each Gameweek's own seeded
        # lineage -> UNAVAILABLE, overriding its own projection.
        assert by_id[2].role_state == UNAVAILABLE
