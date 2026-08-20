"""Compare no transfer with all legal, affordable single transfers for one Gameweek."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from fpl_model.decision.transfer import TransferOption, recommend_single_transfers
from fpl_model.decision.transfer_store import load_transfer_inputs
from fpl_model.storage import DEFAULT_DATABASE_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--squad-snapshot-id", required=True)
    parser.add_argument("--model-run-id", required=True)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _player(player) -> dict[str, object] | None:
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
    }


def _option(option: TransferOption) -> dict[str, object]:
    return {
        "decision": "no_transfer" if option.is_no_transfer else "transfer",
        "outgoing": _player(option.outgoing),
        "incoming": _player(option.incoming),
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
    with duckdb.connect(str(args.database), read_only=True) as connection:
        inputs, targets, excluded_missing_projection, excluded_unavailable = load_transfer_inputs(
            connection,
            squad_snapshot_id=args.squad_snapshot_id,
            model_run_id=args.model_run_id,
        )
    recommendation = recommend_single_transfers(
        inputs.squad,
        inputs.projections,
        targets,
        top_n=args.top_n,
    )
    result = {
        "squad_snapshot_id": inputs.squad_snapshot_id,
        "model_run_id": inputs.model_run_id,
        "target_gameweek": inputs.target_gameweek,
        "recommended": _option(recommendation.recommended),
        "no_transfer": _option(recommendation.no_transfer),
        "transfer_alternatives": [
            _option(option) for option in recommendation.transfer_alternatives
        ],
        "candidate_accounting": {
            "targets_with_projection": len(targets),
            "players_excluded_missing_projection": excluded_missing_projection,
            "players_excluded_unavailable": excluded_unavailable,
            "same_position_swaps_considered": recommendation.candidates_considered,
            "swaps_rejected_budget": recommendation.candidates_rejected_budget,
            "swaps_rejected_constraints": recommendation.candidates_rejected_constraints,
        },
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
