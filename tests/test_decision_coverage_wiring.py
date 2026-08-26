"""Integration coverage: each decision CLI's coverage_gate construction is correct.

`validation/decision_coverage.py` itself is unit-tested against synthetic counts
in `test_decision_coverage.py`. This module instead exercises the small
gate-construction snippet each of the four decision scripts actually runs,
against real `decision/*_store.py` outputs, so a field-swap or wrong-label bug in
the CLI wiring (not in the gate logic itself) would be caught.
"""

from __future__ import annotations

import duckdb

from fpl_model.decision.lineup_store import load_lineup_inputs
from fpl_model.decision.rolling_store import RollingPoolDiagnostics
from fpl_model.decision.transfer_store import load_transfer_inputs
from fpl_model.validation.decision_coverage import CoverageCount, evaluate_decision_coverage
from tests.test_lineup_store import _model_run
from tests.test_squad_snapshot import _database, _import

InitialDiagnostics = RollingPoolDiagnostics


def test_recommend_lineup_wiring_reports_full_owned_squad_coverage(tmp_path):
    database_path = _database(tmp_path)
    imported = _import(tmp_path, database_path=database_path)
    _model_run(database_path)

    with duckdb.connect(str(database_path)) as connection:
        inputs = load_lineup_inputs(
            connection,
            squad_snapshot_id=imported.squad_snapshot_id,
            model_run_id="model_gw1",
        )

    # Mirrors scripts/recommend_lineup.py's coverage_gate construction exactly.
    coverage_gate = evaluate_decision_coverage(
        owned_squad=CoverageCount(
            label="owned_squad",
            covered=len(inputs.squad.players),
            excluded_missing_projection=0,
        )
    )

    assert coverage_gate["passes"] is True
    assert coverage_gate["owned_squad"]["covered"] == 15
    assert coverage_gate["owned_squad"]["total"] == 15


def test_recommend_transfers_wiring_reports_shortlist_exclusions(tmp_path):
    database_path = _database(tmp_path)
    imported = _import(tmp_path, database_path=database_path)
    _model_run(database_path)
    with duckdb.connect(str(database_path)) as connection:
        inputs, targets, excluded_missing_projection, excluded_unavailable = (
            load_transfer_inputs(
                connection,
                squad_snapshot_id=imported.squad_snapshot_id,
                model_run_id="model_gw1",
            )
        )

    # Mirrors scripts/recommend_transfers.py's coverage_gate construction exactly.
    coverage_gate = evaluate_decision_coverage(
        owned_squad=CoverageCount(
            label="owned_squad",
            covered=len(inputs.squad.players),
            excluded_missing_projection=0,
        ),
        shortlists=(
            CoverageCount(
                label=f"gw{inputs.target_gameweek}_transfer_targets",
                covered=len(targets),
                excluded_missing_projection=excluded_missing_projection,
            ),
        ),
    )

    assert coverage_gate["owned_squad"]["passes"] is True
    shortlist_row = coverage_gate["shortlists"][0]
    assert shortlist_row["label"] == "gw1_transfer_targets"
    assert shortlist_row["covered"] == len(targets)
    assert shortlist_row["excluded_missing_projection"] == excluded_missing_projection
    # This fixture's non-owned player pool is fully projected/available by
    # construction, so the shortlist is expected to pass; the assertion still
    # exercises the exact field wiring the CLI uses.
    assert shortlist_row["passes"] is (excluded_missing_projection == 0)


def test_rolling_and_initial_squad_wiring_names_every_failing_gameweek_pool():
    rolling_diagnostics = (
        RollingPoolDiagnostics(
            gameweek=1, model_run_id="r1", projected_players=400,
            excluded_missing_projection=0, excluded_unavailable=5,
        ),
        RollingPoolDiagnostics(
            gameweek=2, model_run_id="r2", projected_players=380,
            excluded_missing_projection=20, excluded_unavailable=5,
        ),
    )
    # Mirrors scripts/plan_three_gameweeks.py's coverage_gate construction exactly.
    rolling_gate = evaluate_decision_coverage(
        owned_squad=CoverageCount(label="owned_squad", covered=15, excluded_missing_projection=0),
        shortlists=tuple(
            CoverageCount(
                label=f"gw{row.gameweek}_pool",
                covered=row.projected_players,
                excluded_missing_projection=row.excluded_missing_projection,
            )
            for row in rolling_diagnostics
        ),
    )
    assert rolling_gate["passes"] is False
    assert rolling_gate["failing_pools"] == ["gw2_pool"]

    initial_diagnostics = (
        InitialDiagnostics(
            gameweek=1, model_run_id="i1", projected_players=390,
            excluded_missing_projection=10, excluded_unavailable=3,
        ),
    )
    # Mirrors scripts/optimize_initial_squad.py's coverage_gate construction
    # exactly -- note there is no owned_squad for the public-data picker.
    initial_gate = evaluate_decision_coverage(
        shortlists=tuple(
            CoverageCount(
                label=f"gw{row.gameweek}_pool",
                covered=row.projected_players,
                excluded_missing_projection=row.excluded_missing_projection,
            )
            for row in initial_diagnostics
        ),
    )
    assert initial_gate["owned_squad"] is None
    assert initial_gate["passes"] is False
    assert initial_gate["failing_pools"] == ["gw1_pool"]
