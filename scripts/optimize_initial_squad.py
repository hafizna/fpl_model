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
    SquadConstraints,
    optimize_initial_squad,
)
from fpl_model.decision.initial_squad_dominance import audit_dominance
from fpl_model.decision.initial_squad_store import load_initial_squad_inputs
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
    parser.add_argument(
        "--lock",
        dest="locked_fpl_ids",
        action="append",
        type=int,
        default=[],
        metavar="FPL_ID",
        help="Force this player into every returned squad (repeatable). For scenario "
        "comparisons such as Haaland/no-Haaland.",
    )
    parser.add_argument(
        "--exclude",
        dest="excluded_fpl_ids",
        action="append",
        type=int,
        default=[],
        metavar="FPL_ID",
        help="Force this player out of every returned squad (repeatable). For scenario "
        "comparisons such as set-and-forget/rotating goalkeeper.",
    )
    parser.add_argument(
        "--audit-dominance",
        action="store_true",
        help="Re-run the search with the most expensive goalkeeper and every never-started "
        "expensive bench player excluded, and flag the recommendation if a cheaper-or-equal, "
        "higher-xPts counterfactual squad exists. Runs the full search up to two additional "
        "times, so it is opt-in.",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--skip-release-validation",
        action="store_true",
        help="Skip the manifest/freshness release gate. RESEARCH/DEVELOPMENT USE ONLY -- "
        "an operational recommendation must consume a release that passes this gate.",
    )
    return parser.parse_args()


def _plan(
    plan: InitialSquadPlan,
    transparency_by_gameweek: dict[int, dict[int, object]],
) -> dict[str, object]:
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
                "captain_transparency": transparency_report(
                    transparency_by_gameweek.get(row.gameweek, {}).get(
                        row.lineup.captain.fpl_id
                    )
                ),
                "vice_captain_fpl_id": row.lineup.vice_captain.fpl_id,
                "starters": [player.fpl_id for player in row.lineup.starters],
                "starters_transparency": [
                    transparency_report(
                        transparency_by_gameweek.get(row.gameweek, {}).get(player.fpl_id)
                    )
                    for player in row.lineup.starters
                ],
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
    ordered_run_ids = tuple(model_run_ids[gameweek] for gameweek in sorted(model_run_ids))

    release_gate_result = None
    if args.skip_release_validation:
        print("WARNING: --skip-release-validation set; release gate not enforced.")
    else:
        try:
            release_gate_result = enforce_release_gate(
                model_run_ids=ordered_run_ids, database_path=args.database
            )
        except ReleaseGateFailure as failure:
            print(str(failure))
            raise SystemExit(1) from failure

    with duckdb.connect(str(args.database), read_only=True) as connection:
        inputs = load_initial_squad_inputs(connection, model_run_ids=model_run_ids)
        transparency_by_gameweek = {
            gameweek: load_player_transparency(
                connection,
                model_run_id=run_id,
                fpl_ids=tuple(target.player.fpl_id for target in pool.players),
            )
            for (gameweek, run_id), pool in zip(inputs.model_run_ids, inputs.pools, strict=True)
        }
    constraints = SquadConstraints(
        locked_fpl_ids=frozenset(args.locked_fpl_ids),
        excluded_fpl_ids=frozenset(args.excluded_fpl_ids),
    )
    result = optimize_initial_squad(
        inputs.pools,
        budget_tenths=args.budget,
        beam_width=args.beam_width,
        candidates_per_position_per_lens=args.candidates_per_position_per_lens,
        returned_squads=args.returned_squads,
        constraints=constraints,
    )
    coverage_gate = evaluate_decision_coverage(
        shortlists=tuple(
            CoverageCount(
                label=f"gw{row.gameweek}_pool",
                covered=row.projected_players,
                excluded_missing_projection=row.excluded_missing_projection,
            )
            for row in inputs.diagnostics
        ),
    )
    release_health = (
        None
        if release_gate_result is None
        else determine_release_health(
            orchestration_report=release_gate_result.report, coverage_gates=(coverage_gate,)
        ).report
    )
    dominance_audit = (
        None
        if not args.audit_dominance
        else audit_dominance(
            result,
            inputs.pools,
            budget_tenths=args.budget,
            beam_width=args.beam_width,
            candidates_per_position_per_lens=args.candidates_per_position_per_lens,
        ).report
    )
    output = {
        "source_ingestion_run_id": inputs.source_ingestion_run_id,
        "model_run_ids": dict(inputs.model_run_ids),
        "planning_as_of": inputs.planning_as_of.isoformat(),
        "model_version": inputs.model_version,
        "recommended": _plan(result.recommended, transparency_by_gameweek),
        "alternatives": [
            _plan(plan, transparency_by_gameweek) for plan in result.alternatives
        ],
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
        "coverage_gate": coverage_gate,
        "release_health": release_health,
        "constraints": {
            "locked_fpl_ids": sorted(constraints.locked_fpl_ids),
            "excluded_fpl_ids": sorted(constraints.excluded_fpl_ids),
        },
        "dominance_audit": dominance_audit,
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
