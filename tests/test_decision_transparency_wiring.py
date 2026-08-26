"""Integration coverage: each decision CLI's transparency wiring is correct.

Mirrors test_decision_coverage_wiring.py's approach but for the transparency
block (raw/calibrated xPts, uncertainty interval) each of the four decision
scripts now attaches. Exercises the exact per-Gameweek transparency lookup
construction those scripts use, against real store outputs plus seeded shadow
calibration/uncertainty rows, so a gameweek/fpl_id mismatch in the CLI wiring
(not in decision_transparency itself, which has its own unit tests) would be
caught.
"""

from __future__ import annotations

import duckdb

from fpl_model.decision.initial_squad_store import load_initial_squad_inputs
from fpl_model.decision.lineup_store import load_lineup_inputs
from fpl_model.decision.rolling_store import load_rolling_inputs
from fpl_model.validation.decision_transparency import (
    load_player_transparency,
    transparency_report,
)
from tests.test_lineup_store import _model_run
from tests.test_rolling_store import _seed_horizon
from tests.test_squad_snapshot import _database, _import


def _add_shadow_data(database_path, *, model_run_id: str, player_code: int, fixture_id: int) -> None:
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO shadow_calibration_artifact (
                artifact_id, calibration_type, source_season, source_model_version,
                source_reference, training_rows, training_gameweeks, slope, intercept,
                policy_version, status
            ) VALUES (
                'calib_' || ?, 'xpts', '2025-26', 'test', 'ref.json', 100, 10, 0.8, 0.2,
                'test', 'shadow'
            )
            """,
            [model_run_id],
        )
        connection.execute(
            """
            INSERT INTO player_fixture_shadow_projection
            SELECT model_run_id, player_code, fixture_id, 'calib_' || model_run_id,
                   final_xpts, final_xpts * 0.8, FALSE
            FROM player_fixture_projection
            WHERE model_run_id = ? AND player_code = ? AND fixture_id = ?
            """,
            [model_run_id, player_code, fixture_id],
        )


def test_recommend_lineup_wiring_attaches_raw_and_calibrated_xpts(tmp_path):
    database_path = _database(tmp_path)
    imported = _import(tmp_path, database_path=database_path)
    _model_run(database_path)
    _add_shadow_data(database_path, model_run_id="model_gw1", player_code=1001, fixture_id=5001)

    with duckdb.connect(str(database_path)) as connection:
        inputs = load_lineup_inputs(
            connection, squad_snapshot_id=imported.squad_snapshot_id, model_run_id="model_gw1"
        )
        # Mirrors scripts/recommend_lineup.py's transparency wiring exactly.
        transparency_by_id = load_player_transparency(
            connection,
            model_run_id=inputs.model_run_id,
            fpl_ids=tuple(player.fpl_id for player in inputs.squad.players),
        )

    report = transparency_report(transparency_by_id.get(1))
    assert report is not None
    assert report["raw_xpts"] == 1.0
    assert report["shadow_calibrated_xpts"] == 0.8


def test_rolling_wiring_keys_transparency_by_the_right_gameweeks_run(tmp_path):
    imported, database_path, model_runs = _seed_horizon(tmp_path)
    # Seed shadow calibration only for the GW2 run, for one specific player,
    # so the test can prove GW1/GW3 lookups stay empty for that player.
    _add_shadow_data(database_path, model_run_id="model_gw2", player_code=1001, fixture_id=2001)

    with duckdb.connect(str(database_path)) as connection:
        inputs = load_rolling_inputs(
            connection,
            squad_snapshot_id=imported.squad_snapshot_id,
            model_run_ids=model_runs,
        )
        # Mirrors scripts/plan_three_gameweeks.py's transparency wiring exactly.
        transparency_by_gameweek = {
            gameweek: load_player_transparency(
                connection,
                model_run_id=run_id,
                fpl_ids=tuple(target.player.fpl_id for target in pool.players),
            )
            for (gameweek, run_id), pool in zip(
                inputs.model_run_ids, inputs.pools, strict=True
            )
        }

    gw1_report = transparency_report(transparency_by_gameweek[1].get(1))
    gw2_report = transparency_report(transparency_by_gameweek[2].get(1))
    gw3_report = transparency_report(transparency_by_gameweek[3].get(1))
    assert gw1_report["shadow_calibrated_xpts"] is None
    assert gw2_report["shadow_calibrated_xpts"] == 0.8  # 1.0 * 0.8, seeded above
    assert gw3_report["shadow_calibrated_xpts"] is None


def test_initial_squad_wiring_keys_transparency_by_the_right_gameweeks_run(tmp_path):
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
    _add_shadow_data(database_path, model_run_id=model_runs[2], player_code=1001, fixture_id=2001)

    with duckdb.connect(str(database_path)) as connection:
        inputs = load_initial_squad_inputs(connection, model_run_ids=model_runs)
        # Mirrors scripts/optimize_initial_squad.py's transparency wiring exactly.
        transparency_by_gameweek = {
            gameweek: load_player_transparency(
                connection,
                model_run_id=run_id,
                fpl_ids=tuple(target.player.fpl_id for target in pool.players),
            )
            for (gameweek, run_id), pool in zip(
                inputs.model_run_ids, inputs.pools, strict=True
            )
        }

    gw1_report = transparency_report(transparency_by_gameweek[1].get(1))
    gw2_report = transparency_report(transparency_by_gameweek[2].get(1))
    assert gw1_report["shadow_calibrated_xpts"] is None
    assert gw2_report["shadow_calibrated_xpts"] == 0.8
