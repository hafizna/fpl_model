"""Integration coverage: recommend_transfers.py's role_state wiring is correct.

Mirrors test_role_state_wiring.py's approach for recommend_lineup.py: exercises
the exact per-squad role state lookup construction scripts/recommend_transfers.py
uses (load_role_states over the union of owned-squad and transfer-target
fpl_ids), against real store outputs plus a seeded availability/appearance
lineage. This is the P0 sign-off gap this session closed: recommend_transfers.py
previously carried `transparency` but not `role_state`, so a transfer decision
involving a ROTATION-state player carried no visible warning.
"""

from __future__ import annotations

import duckdb

from fpl_model.decision.transfer_store import load_transfer_inputs
from fpl_model.validation.role_state import LIKELY_STARTER, UNAVAILABLE, load_role_states
from tests.test_lineup_store import _model_run
from tests.test_role_state_wiring import _add_role_state_lineage
from tests.test_squad_snapshot import _database, _import


def test_recommend_transfers_wiring_attaches_role_state_to_owned_squad_players(tmp_path):
    database_path = _database(tmp_path)
    imported = _import(tmp_path, database_path=database_path)
    _model_run(database_path)
    _add_role_state_lineage(
        database_path, model_run_id="model_gw1", source_ingestion_run_id="official", gameweek=1
    )

    with duckdb.connect(str(database_path)) as connection:
        inputs, targets, _excluded_missing, _excluded_unavailable = load_transfer_inputs(
            connection, squad_snapshot_id=imported.squad_snapshot_id, model_run_id="model_gw1"
        )
        # Mirrors scripts/recommend_transfers.py's role_state wiring exactly.
        relevant_fpl_ids = tuple(
            {player.fpl_id for player in inputs.squad.players}
            | {target.player.fpl_id for target in targets}
        )
        role_state_by_id = load_role_states(
            connection, model_run_id=inputs.model_run_id, fpl_ids=relevant_fpl_ids
        )

    # fpl_id=1 (owned squad) is resolved eligible with start_probability=0.8
    # in _model_run's own seed data -> LIKELY_STARTER.
    assert role_state_by_id[1].role_state == LIKELY_STARTER
    # fpl_id=2 (owned squad) is resolved ineligible -> UNAVAILABLE, overriding
    # its own projection -- exactly the material-role-conflict shape the P0
    # sign-off item requires be visible on a transfer decision, not only a
    # lineup recommendation.
    assert role_state_by_id[2].role_state == UNAVAILABLE
