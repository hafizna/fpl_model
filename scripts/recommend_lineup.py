"""Recommend a legal starting XI, bench order, captain, and vice-captain."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from fpl_model.decision.autosub import compute_expected_autosub_value
from fpl_model.decision.lineup import recommend_lineup
from fpl_model.decision.lineup_store import load_lineup_inputs
from fpl_model.decision.role_scenario_sensitivity import evaluate_role_scenario_sensitivity
from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.decision_coverage import CoverageCount, evaluate_decision_coverage
from fpl_model.validation.decision_transparency import (
    load_player_transparency,
    transparency_report,
)
from fpl_model.validation.release_health import determine_release_health
from fpl_model.validation.release_orchestration import (
    ReleaseGateFailure,
    enforce_release_gate,
)
from fpl_model.validation.role_state import load_role_states, role_state_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--squad-snapshot-id", required=True)
    parser.add_argument("--model-run-id", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--skip-release-validation",
        action="store_true",
        help="Skip the manifest/freshness release gate. RESEARCH/DEVELOPMENT USE ONLY -- "
        "an operational recommendation must consume a release that passes this gate.",
    )
    return parser.parse_args()


def _player(player, projection_by_id, transparency_by_id, role_state_by_id) -> dict[str, object]:
    projection = projection_by_id[player.fpl_id]
    return {
        "fpl_id": player.fpl_id,
        "player_code": player.player_code,
        "player_name": player.player_name,
        "team_id": player.team_id,
        "position": player.position,
        "expected_points": projection.expected_points,
        "uncertainty": projection.uncertainty,
        "data_quality_flags": list(projection.data_quality_flags),
        "transparency": transparency_report(transparency_by_id.get(player.fpl_id)),
        "role_state": role_state_report(role_state_by_id.get(player.fpl_id)),
    }


def main() -> None:
    args = parse_args()
    release_gate_result = None
    if args.skip_release_validation:
        print("WARNING: --skip-release-validation set; release gate not enforced.")
    else:
        try:
            release_gate_result = enforce_release_gate(
                model_run_ids=(args.model_run_id,), database_path=args.database
            )
        except ReleaseGateFailure as failure:
            print(str(failure))
            raise SystemExit(1) from failure

    with duckdb.connect(str(args.database), read_only=True) as connection:
        inputs = load_lineup_inputs(
            connection,
            squad_snapshot_id=args.squad_snapshot_id,
            model_run_id=args.model_run_id,
        )
        transparency_by_id = load_player_transparency(
            connection,
            model_run_id=inputs.model_run_id,
            fpl_ids=tuple(player.fpl_id for player in inputs.squad.players),
        )
        role_state_by_id = load_role_states(
            connection,
            model_run_id=inputs.model_run_id,
            fpl_ids=tuple(player.fpl_id for player in inputs.squad.players),
        )
    recommendation = recommend_lineup(inputs.squad, inputs.projections)
    projection_by_id = {projection.fpl_id: projection for projection in inputs.projections}
    autosub_value = compute_expected_autosub_value(recommendation, projection_by_id)
    role_scenario_sensitivity = evaluate_role_scenario_sensitivity(
        inputs.squad,
        tuple(inputs.projections),
        role_state_by_id=role_state_by_id,
        base_recommendation=recommendation,
    )
    coverage_gate = evaluate_decision_coverage(
        owned_squad=CoverageCount(
            label="owned_squad",
            covered=len(inputs.squad.players),
            excluded_missing_projection=0,
        )
    )
    release_health = (
        None
        if release_gate_result is None
        else determine_release_health(
            orchestration_report=release_gate_result.report, coverage_gates=(coverage_gate,)
        ).report
    )
    result = {
        "squad_snapshot_id": inputs.squad_snapshot_id,
        "model_run_id": inputs.model_run_id,
        "target_gameweek": inputs.target_gameweek,
        "formation": recommendation.formation,
        "starting_xpts": recommendation.starting_xpts,
        "captain_bonus_xpts": recommendation.captain_bonus_xpts,
        "total_xpts": recommendation.total_xpts,
        "uncertainty": recommendation.uncertainty,
        "captain": _player(
            recommendation.captain, projection_by_id, transparency_by_id, role_state_by_id
        ),
        "vice_captain": _player(
            recommendation.vice_captain, projection_by_id, transparency_by_id, role_state_by_id
        ),
        "starters": [
            _player(player, projection_by_id, transparency_by_id, role_state_by_id)
            for player in recommendation.starters
        ],
        "bench_goalkeeper": _player(
            recommendation.bench_goalkeeper,
            projection_by_id,
            transparency_by_id,
            role_state_by_id,
        ),
        "outfield_bench_order": [
            _player(player, projection_by_id, transparency_by_id, role_state_by_id)
            for player in recommendation.outfield_bench_order
        ],
        "data_quality_flags": list(recommendation.data_quality_flags),
        "expected_autosub_value": {
            "bench_goalkeeper": {
                "fpl_id": autosub_value.bench_goalkeeper.fpl_id,
                "expected_value": autosub_value.bench_goalkeeper.expected_value,
                "usage_probability": autosub_value.bench_goalkeeper.usage_probability,
            },
            "outfield_bench": [
                {
                    "fpl_id": row.fpl_id,
                    "expected_value": row.expected_value,
                    "usage_probability": row.usage_probability,
                }
                for row in autosub_value.outfield_bench
            ],
            "total_expected_bench_value": autosub_value.total_expected_bench_value,
            "note": (
                "Diagnostic only: each bench slot's expected xPts contribution under FPL's "
                "real autosub rule (0-minute blanks, bench order, formation-legal "
                "substitutions). Never added to total_xpts."
            ),
        },
        "coverage_gate": coverage_gate,
        "release_health": release_health,
        "role_scenario_sensitivity": role_scenario_sensitivity.report,
        "method_note": (
            "Exhaustive maximum-mean-xPts search across every legal XI; captain is the "
            "highest-xPts starter and vice-captain the second highest. Missing squad "
            "projections are rejected, never treated as zero. `role_scenario_sensitivity` "
            "labels this recommendation `sensitive` rather than an unconditional best "
            "option when a ROTATION-state player blanking would change the starting XI "
            "or captain -- see that field for which player(s) drive the warning."
        ),
    }
    output = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
