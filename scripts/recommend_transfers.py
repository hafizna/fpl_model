"""Compare no transfer with all legal, affordable single transfers for one Gameweek."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from fpl_model.decision.transfer import TransferOption, recommend_single_transfers
from fpl_model.decision.transfer_dominance import audit_goalkeeper_reinvestment
from fpl_model.decision.transfer_store import load_transfer_inputs
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
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--audit-goalkeeper-reinvestment",
        action="store_true",
        help="Check whether selling the squad's most expensive backup goalkeeper for the "
        "cheapest legal replacement, then reinvesting the freed bank into the single best "
        "target, dominates the recommended option. Runs one extra search pass.",
    )
    parser.add_argument(
        "--skip-release-validation",
        action="store_true",
        help="Skip the manifest/freshness release gate. RESEARCH/DEVELOPMENT USE ONLY -- "
        "an operational recommendation must consume a release that passes this gate.",
    )
    return parser.parse_args()


def _player(player, transparency_by_id, role_state_by_id) -> dict[str, object] | None:
    if player is None:
        return None
    return {
        "fpl_id": player.fpl_id,
        "player_code": player.player_code,
        "player_name": player.player_name,
        "team_id": player.team_id,
        "position": player.position,
        "current_price_tenths": player.current_price_tenths,
        "selling_price_tenths": player.selling_price_tenths,
        "transparency": transparency_report(transparency_by_id.get(player.fpl_id)),
        "role_state": role_state_report(role_state_by_id.get(player.fpl_id)),
    }


def _option(option: TransferOption, transparency_by_id, role_state_by_id) -> dict[str, object]:
    return {
        "decision": "no_transfer" if option.is_no_transfer else "transfer",
        "outgoing": _player(option.outgoing, transparency_by_id, role_state_by_id),
        "incoming": _player(option.incoming, transparency_by_id, role_state_by_id),
        "bank_after_tenths": option.bank_after_tenths,
        "transfer_cost": option.transfer_cost,
        "gross_xpts_gain": option.gross_xpts_gain,
        "net_xpts_gain": option.net_xpts_gain,
        "post_decision_total_xpts": option.lineup.total_xpts,
        "formation": option.lineup.formation,
        "captain_fpl_id": option.lineup.captain.fpl_id,
        "vice_captain_fpl_id": option.lineup.vice_captain.fpl_id,
        "data_quality_flags": list(option.lineup.data_quality_flags),
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
        inputs, targets, excluded_missing_projection, excluded_unavailable = load_transfer_inputs(
            connection,
            squad_snapshot_id=args.squad_snapshot_id,
            model_run_id=args.model_run_id,
        )
        relevant_fpl_ids = tuple(
            {player.fpl_id for player in inputs.squad.players}
            | {target.player.fpl_id for target in targets}
        )
        transparency_by_id = load_player_transparency(
            connection,
            model_run_id=inputs.model_run_id,
            fpl_ids=relevant_fpl_ids,
        )
        role_state_by_id = load_role_states(
            connection,
            model_run_id=inputs.model_run_id,
            fpl_ids=relevant_fpl_ids,
        )
    recommendation = recommend_single_transfers(
        inputs.squad,
        inputs.projections,
        targets,
        top_n=args.top_n,
    )
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
    release_health = (
        None
        if release_gate_result is None
        else determine_release_health(
            orchestration_report=release_gate_result.report, coverage_gates=(coverage_gate,)
        ).report
    )
    goalkeeper_reinvestment_audit = (
        None
        if not args.audit_goalkeeper_reinvestment
        else audit_goalkeeper_reinvestment(
            inputs.squad,
            inputs.projections,
            targets,
            recommendation=recommendation,
        ).report
    )
    result = {
        "squad_snapshot_id": inputs.squad_snapshot_id,
        "model_run_id": inputs.model_run_id,
        "target_gameweek": inputs.target_gameweek,
        "recommended": _option(recommendation.recommended, transparency_by_id, role_state_by_id),
        "no_transfer": _option(recommendation.no_transfer, transparency_by_id, role_state_by_id),
        "transfer_alternatives": [
            _option(option, transparency_by_id, role_state_by_id)
            for option in recommendation.transfer_alternatives
        ],
        "candidate_accounting": {
            "targets_with_projection": len(targets),
            "players_excluded_missing_projection": excluded_missing_projection,
            "players_excluded_unavailable": excluded_unavailable,
            "same_position_swaps_considered": recommendation.candidates_considered,
            "swaps_rejected_budget": recommendation.candidates_rejected_budget,
            "swaps_rejected_constraints": recommendation.candidates_rejected_constraints,
        },
        "coverage_gate": coverage_gate,
        "release_health": release_health,
        "goalkeeper_reinvestment_audit": goalkeeper_reinvestment_audit,
        "method_note": (
            "Single-Gameweek exhaustive comparison of no transfer and every affordable, "
            "same-position single transfer. Each legal resulting squad receives a fresh "
            "optimal-XI and captain search; a four-point hit applies when no free transfer "
            "is available. Players without projections are excluded and counted."
        ),
        "limitations": [
            "This is a one-Gameweek mean-xPts recommendation, not a multi-Gameweek plan.",
            "It does not model future free-transfer value, price changes, chips, or risk preference.",
        ],
    }
    output = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
