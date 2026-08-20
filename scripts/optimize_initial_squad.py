"""Choose an explainable initial FPL squad over a frozen three-GW horizon."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

import duckdb

from fpl_model.decision.initial_squad import (
    DEFAULT_CANDIDATES_PER_POSITION_PER_LENS,
    DEFAULT_INITIAL_BUDGET_TENTHS,
    DEFAULT_INITIAL_SQUAD_BEAM_WIDTH,
    DEFAULT_RETURNED_INITIAL_SQUADS,
    InitialSquadPlan,
    optimize_initial_squad,
)
from fpl_model.decision.initial_squad_store import load_initial_squad_inputs
from fpl_model.storage import DEFAULT_DATABASE_PATH


def _model_run(value: str) -> tuple[int, str]:
    try:
        gameweek_text, model_run_id = value.split("=", 1)
        gameweek = int(gameweek_text.removeprefix("GW").removeprefix("gw"))
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("model run must use GW=MODEL_RUN_ID") from exc
    if not 1 <= gameweek <= 38 or not model_run_id.strip():
        raise argparse.ArgumentTypeError("model run must use GW=MODEL_RUN_ID")
    return gameweek, model_run_id.strip()


def _budget(value: str) -> int:
    try:
        decimal = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("budget must be a decimal amount such as 100.0") from exc
    tenths = decimal * 10
    if decimal <= 0 or tenths != tenths.to_integral_value():
        raise argparse.ArgumentTypeError("budget must be positive with at most one decimal place")
    return int(tenths)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-run",
        action="append",
        type=_model_run,
        required=True,
        metavar="GW=MODEL_RUN_ID",
        help="Repeat for exactly three consecutive, frozen Gameweeks",
    )
    parser.add_argument(
        "--budget",
        type=_budget,
        default=DEFAULT_INITIAL_BUDGET_TENTHS,
        metavar="MILLIONS",
    )
    parser.add_argument("--beam-width", type=int, default=DEFAULT_INITIAL_SQUAD_BEAM_WIDTH)
    parser.add_argument(
        "--candidates-per-position-per-lens",
        type=int,
        default=DEFAULT_CANDIDATES_PER_POSITION_PER_LENS,
    )
    parser.add_argument(
        "--returned-squads",
        type=int,
        default=DEFAULT_RETURNED_INITIAL_SQUADS,
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _plan(plan: InitialSquadPlan) -> dict[str, object]:
    return {
        "cumulative_xpts": plan.cumulative_xpts,
        "uncertainty": plan.uncertainty,
        "squad_cost_tenths": plan.squad_cost_tenths,
        "bank_tenths": plan.bank_tenths,
        "data_quality_flags": list(plan.data_quality_flags),
        "squad": [
            {
                "fpl_id": player.fpl_id,
                "player_code": player.player_code,
                "name": player.player_name,
                "team_id": player.team_id,
                "position": player.position,
                "price_tenths": player.current_price_tenths,
            }
            for player in plan.squad.players
        ],
        "gameweeks": [
            {
                "gameweek": row.gameweek,
                "total_xpts": row.lineup.total_xpts,
                "starting_xpts": row.lineup.starting_xpts,
                "captain_bonus_xpts": row.lineup.captain_bonus_xpts,
                "formation": row.lineup.formation,
                "captain_fpl_id": row.lineup.captain.fpl_id,
                "captain_name": row.lineup.captain.player_name,
                "vice_captain_fpl_id": row.lineup.vice_captain.fpl_id,
                "starters": [player.fpl_id for player in row.lineup.starters],
                "bench_goalkeeper": row.lineup.bench_goalkeeper.fpl_id,
                "outfield_bench_order": [
                    player.fpl_id for player in row.lineup.outfield_bench_order
                ],
            }
            for row in plan.gameweeks
        ],
    }


def main() -> None:
    args = parse_args()
    model_run_ids = dict(args.model_run)
    if len(model_run_ids) != len(args.model_run):
        raise ValueError("each Gameweek may be specified only once")
    with duckdb.connect(str(args.database), read_only=True) as connection:
        inputs = load_initial_squad_inputs(connection, model_run_ids=model_run_ids)
    result = optimize_initial_squad(
        inputs.pools,
        budget_tenths=args.budget,
        beam_width=args.beam_width,
        candidates_per_position_per_lens=args.candidates_per_position_per_lens,
        returned_squads=args.returned_squads,
    )
    output = {
        "source_ingestion_run_id": inputs.source_ingestion_run_id,
        "model_run_ids": dict(inputs.model_run_ids),
        "planning_as_of": inputs.planning_as_of.isoformat(),
        "model_version": inputs.model_version,
        "recommended": _plan(result.recommended),
        "alternatives": [_plan(plan) for plan in result.alternatives],
        "coverage": [
            {
                "gameweek": row.gameweek,
                "model_run_id": row.model_run_id,
                "projected_players": row.projected_players,
                "excluded_missing_projection": row.excluded_missing_projection,
                "excluded_unavailable": row.excluded_unavailable,
            }
            for row in inputs.diagnostics
        ],
        "search": {
            "search_is_exact": result.search_is_exact,
            "beam_width": result.beam_width,
            "candidates_per_position_per_lens": result.candidates_per_position_per_lens,
            "fully_projected_transferable_players": len(result.eligible_player_ids),
            "candidate_players_after_pruning": len(result.candidate_player_ids),
            "complete_squads_evaluated": result.complete_squads_evaluated,
        },
        "method_note": (
            "Approximate candidate-pruned beam search. Candidate retention uses horizon xPts, "
            "horizon xPts per price, and cheap-enabler lenses. Every completed squad is checked "
            "against the exact initial budget, 2/5/5/3 positions, max-three-per-club rule, and "
            "then rescored with an exhaustive legal XI/captain search in every Gameweek."
        ),
        "limitations": [
            "The bounded search is not a proof of the global optimum.",
            "Bench Boost, Triple Captain, future transfers, and future price changes are excluded.",
            "All three model runs share frozen preseason player-rate, appearance, and team inputs.",
            "Refresh the public FPL snapshot and rebuild the horizon before an operational pick.",
        ],
    }
    text = json.dumps(output, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
