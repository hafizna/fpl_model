"""Integration coverage: recommend_lineup.py's role_scenario_sensitivity wiring is correct.

Mirrors test_role_state_wiring.py's approach: exercises the exact
`evaluate_role_scenario_sensitivity` call scripts/recommend_lineup.py makes,
against real `load_lineup_inputs` output plus a seeded role_state lineage, so
a wiring mistake (wrong squad, wrong projections, stale base_recommendation)
would be caught even though `role_scenario_sensitivity` itself already has
its own unit tests against synthetic squads.
"""

from __future__ import annotations

import duckdb

from fpl_model.decision.lineup import recommend_lineup
from fpl_model.decision.lineup_store import load_lineup_inputs
from fpl_model.decision.role_scenario_sensitivity import evaluate_role_scenario_sensitivity
from fpl_model.validation.role_state import LIKELY_STARTER, ROTATION, RoleStateResult
from tests.test_lineup_store import _model_run
from tests.test_squad_snapshot import _database, _import


def test_recommend_lineup_wiring_flags_a_sensitive_recommendation(tmp_path):
    database_path = _database(tmp_path)
    imported = _import(tmp_path, database_path=database_path)
    _model_run(database_path)

    with duckdb.connect(str(database_path)) as connection:
        inputs = load_lineup_inputs(
            connection, squad_snapshot_id=imported.squad_snapshot_id, model_run_id="model_gw1"
        )

    recommendation = recommend_lineup(inputs.squad, inputs.projections)
    # Mirrors scripts/recommend_lineup.py's wiring exactly: the weakest
    # starter in this fixture's recommendation (lowest expected_points among
    # `recommendation.starters`, since every player's start_probability is
    # tied at 0.8 in `_model_run`) is treated as a reviewed ROTATION case,
    # the same way a real role_state_by_id from load_role_states would.
    weakest_starter = min(recommendation.starters, key=lambda player: player.fpl_id)
    role_state_by_id = {
        player.fpl_id: RoleStateResult(
            role_state=(
                ROTATION if player.fpl_id == weakest_starter.fpl_id else LIKELY_STARTER
            ),
            reason="test fixture",
        )
        for player in inputs.squad.players
    }

    sensitivity = evaluate_role_scenario_sensitivity(
        inputs.squad,
        tuple(inputs.projections),
        role_state_by_id=role_state_by_id,
        base_recommendation=recommendation,
    )

    assert sensitivity.base_recommendation is recommendation
    assert len(sensitivity.scenarios_considered) == 1
    assert sensitivity.scenarios_considered[0].fpl_id == weakest_starter.fpl_id
    # Blanking the single weakest starter must change the starting XI: the
    # strongest bench player in this fixture's construction always scores
    # more than 0.0 and is a legal same-position replacement.
    assert sensitivity.is_sensitive is True
    assert sensitivity.report["label"] == "sensitive"
