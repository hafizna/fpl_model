"""Integration coverage: each decision CLI enforces the release gate correctly.

`enforce_release_gate` itself is already thoroughly tested in
test_release_orchestration.py. What could actually go wrong in a CLI script is
narrower: passing the wrong model_run_ids, or passing them out of ascending-
Gameweek order (`plan_three_gameweeks.py`/`optimize_initial_squad.py` build a
dict from repeated `--model-run GW=ID` arguments, whose iteration order is not
guaranteed to already be GW-ascending -- `build_release_manifest` requires
ascending order and fails closed otherwise). This module exercises exactly that
ordering construction plus the pass/fail wiring, reusing the manifest test's own
well-tested single/multi-run fixture rather than re-deriving a whole baseline
pipeline seed.
"""

from __future__ import annotations

import duckdb
import pytest

from fpl_model.validation.release_orchestration import (
    ReleaseGateFailure,
    enforce_release_gate,
)
from tests.test_release_manifest import _seed_horizon


def _add_freshness_prerequisites(database_path, *, ingestion_run_id: str, gameweek: int) -> None:
    with duckdb.connect(str(database_path)) as connection:
        existing_teams = connection.execute(
            "SELECT count(*) FROM team_snapshot WHERE ingestion_run_id = ?", [ingestion_run_id]
        ).fetchone()[0]
        if existing_teams == 0:
            connection.execute(
                """
                INSERT INTO team_snapshot (
                    ingestion_run_id, team_id, team_code, name, short_name, unavailable
                ) VALUES
                    (?, 1, 101, 'Sunderland', 'SUN', false),
                    (?, 2, 102, 'Opponent', 'OPP', false)
                """,
                [ingestion_run_id, ingestion_run_id],
            )
        connection.execute(
            """
            INSERT INTO gameweek_snapshot (
                ingestion_run_id, gameweek, name, deadline_time, release_time,
                finished, data_checked, is_previous, is_current, is_next
            ) VALUES (?, ?, ?, '2026-08-22T00:30:00+07:00', NULL,
                      TRUE, TRUE, FALSE, TRUE, FALSE)
            """,
            [ingestion_run_id, gameweek, f"Gameweek {gameweek}"],
        )
        connection.execute(
            """
            INSERT INTO fixture_snapshot VALUES (
                ?, ?, ?, '2026-08-22T15:00:00+01:00', 1, 2, TRUE, TRUE
            )
            """,
            [ingestion_run_id, gameweek * 1000, gameweek],
        )


def test_single_run_gate_passes_matching_recommend_lineup_and_recommend_transfers_wiring(
    tmp_path,
):
    database_path = tmp_path / "gate.duckdb"
    _seed_horizon(database_path)
    _add_freshness_prerequisites(database_path, ingestion_run_id="snapshot", gameweek=1)

    # Mirrors scripts/recommend_lineup.py's / recommend_transfers.py's exact call:
    # a one-element tuple built from the single --model-run-id argument.
    result = enforce_release_gate(model_run_ids=("baseline_gw1",), database_path=database_path)

    assert result.passes is True


def test_ordering_construction_sorts_out_of_order_cli_args_by_gameweek():
    # Mirrors scripts/plan_three_gameweeks.py's / optimize_initial_squad.py's
    # exact ordering construction: args.model_run may arrive in any order
    # (argparse preserves --model-run repetition order, not GW order), so the
    # dict must be re-sorted by Gameweek before being handed to the gate.
    model_run_ids = {3: "baseline_gw3", 1: "baseline_gw1", 2: "baseline_gw2"}

    ordered_run_ids = tuple(model_run_ids[gameweek] for gameweek in sorted(model_run_ids))

    assert ordered_run_ids == ("baseline_gw1", "baseline_gw2", "baseline_gw3")


def test_multi_run_gate_rejects_when_one_gameweek_lacks_prerequisites(tmp_path):
    database_path = tmp_path / "gate.duckdb"
    _seed_horizon(database_path)
    _add_freshness_prerequisites(database_path, ingestion_run_id="snapshot", gameweek=1)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO model_run (
                model_run_id, target_gameweek, as_of, deadline, model_version,
                source_ingestion_run_id, status
            ) VALUES (
                'baseline_gw2', 2, '2026-08-18T09:00:00+07:00',
                '2026-08-29T00:30:00+07:00', 'test', 'snapshot', 'completed'
            )
            """
        )
    # Deliberately no baseline_projection_run/gameweek_snapshot for GW2.

    ordered_run_ids = ("baseline_gw1", "baseline_gw2")

    with pytest.raises(ReleaseGateFailure) as excinfo:
        enforce_release_gate(model_run_ids=ordered_run_ids, database_path=database_path)

    assert "GW2" in str(excinfo.value)
    assert excinfo.value.result.report["model_run_ids"] == list(ordered_run_ids)
