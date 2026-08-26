"""Build an auditable rolling three-Gameweek squad and transfer plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from fpl_model.decision.rolling import (
    DEFAULT_BEAM_WIDTH,
    DEFAULT_CANDIDATES_PER_POSITION,
    DEFAULT_RETURNED_PLANS,
    RollingPlan,
    plan_three_gameweeks,
)
from fpl_model.decision.rolling_store import load_rolling_inputs
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--squad-snapshot-id", required=True)
    parser.add_argument(
        "--model-run",
        action="append",
        type=_model_run,
        required=True,
        metavar="GW=MODEL_RUN_ID",
        help="Repeat for exactly three consecutive Gameweeks",
    )
    parser.add_argument("--beam-width", type=int, default=DEFAULT_BEAM_WIDTH)
    parser.add_argument(
        "--candidates-per-position",
        type=int,
        default=DEFAULT_CANDIDATES_PER_POSITION,
    )
    parser.add_argument("--returned-plans", type=int, default=DEFAULT_RETURNED_PLANS)
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
    plan: RollingPlan,
    names: dict[int, str],
    transparency_by_gameweek: dict[int, dict[int, object]],
) -> dict[str, object]:
    def _transparency(gameweek: int, fpl_id: int | None) -> dict[str, object] | None:
        if fpl_id is None:
            return None
        return transparency_report(transparency_by_gameweek.get(gameweek, {}).get(fpl_id))

    return {
        "cumulative_net_xpts": plan.cumulative_net_xpts,
        "total_transfer_cost": plan.total_transfer_cost,
        "uncertainty": plan.uncertainty,
        "terminal_bank_tenths": plan.terminal_bank_tenths,
        "terminal_free_transfers": plan.terminal_free_transfers,
        "data_quality_flags": list(plan.data_quality_flags),
        "steps": [
            {
                "gameweek": step.gameweek,
                "decision": step.decision,
                "outgoing_fpl_id": step.outgoing_fpl_id,
                "outgoing_name": names.get(step.outgoing_fpl_id),
                "outgoing_transparency": _transparency(step.gameweek, step.outgoing_fpl_id),
                "incoming_fpl_id": step.incoming_fpl_id,
                "incoming_name": names.get(step.incoming_fpl_id),
                "incoming_transparency": _transparency(step.gameweek, step.incoming_fpl_id),
                "free_transfers_before": step.free_transfers_before,
                "free_transfers_after": step.free_transfers_after,
                "bank_after_tenths": step.bank_after_tenths,
                "transfer_cost": step.transfer_cost,
                "net_gameweek_xpts": step.net_gameweek_xpts,
                "formation": step.lineup.formation,
                "captain_fpl_id": step.lineup.captain.fpl_id,
                "captain_name": step.lineup.captain.player_name,
                "captain_transparency": _transparency(step.gameweek, step.lineup.captain.fpl_id),
                "vice_captain_fpl_id": step.lineup.vice_captain.fpl_id,
                "starters": [player.fpl_id for player in step.lineup.starters],
                "starters_transparency": [
                    _transparency(step.gameweek, player.fpl_id)
                    for player in step.lineup.starters
                ],
                "bench_goalkeeper": step.lineup.bench_goalkeeper.fpl_id,
                "outfield_bench_order": [
                    player.fpl_id for player in step.lineup.outfield_bench_order
                ],
            }
            for step in plan.steps
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
        inputs = load_rolling_inputs(
            connection,
            squad_snapshot_id=args.squad_snapshot_id,
            model_run_ids=model_run_ids,
        )
        transparency_by_gameweek = {
            gameweek: load_player_transparency(
                connection,
                model_run_id=run_id,
                fpl_ids=tuple(target.player.fpl_id for target in pool.players),
            )
            for (gameweek, run_id), pool in zip(inputs.model_run_ids, inputs.pools, strict=True)
        }
    result = plan_three_gameweeks(
        inputs.lineup_inputs.squad,
        inputs.pools,
        beam_width=args.beam_width,
        candidates_per_position=args.candidates_per_position,
        returned_plans=args.returned_plans,
    )
    names = {
        row.player.fpl_id: row.player.player_name
        for pool in inputs.pools
        for row in pool.players
    }
    coverage_gate = evaluate_decision_coverage(
        owned_squad=CoverageCount(
            label="owned_squad",
            covered=len(inputs.lineup_inputs.squad.players),
            excluded_missing_projection=0,
        ),
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
    output = {
        "squad_snapshot_id": inputs.lineup_inputs.squad_snapshot_id,
        "model_run_ids": dict(inputs.model_run_ids),
        "planning_as_of": inputs.planning_as_of.isoformat(),
        "model_version": inputs.model_version,
        "recommended": _plan(result.recommended, names, transparency_by_gameweek),
        "alternatives": [
            _plan(plan, names, transparency_by_gameweek) for plan in result.alternatives
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
        "search": {
            "search_is_exact": result.search_is_exact,
            "beam_width": result.beam_width,
            "candidates_per_position": result.candidates_per_position,
            "eligible_players_with_all_three_gws": len(result.eligible_player_ids),
        },
        "scheduled_replan_after_gameweek": inputs.pools[-1].gameweek,
        "emergency_replan_triggers": [
            "confirmed injury or material availability downgrade",
            "suspension or red card",
            "real-world transfer or registration change",
            "material starting-role change supported by current evidence",
        ],
        "method_note": (
            "Approximate beam search over roll and at most one transfer per Gameweek. "
            "Every retained state uses exact squad, bank, club-limit, free-transfer, hit, "
            "lineup, bench, and captain rules. Candidate pruning uses three-GW projected "
            "points; no future projection is fabricated."
        ),
        "limitations": [
            "The beam/candidate-pruned search is not a proof of the global optimum.",
            "At most one transfer per Gameweek is considered.",
            "Chips, future price changes, and risk-adjusted objectives are not yet modelled.",
            "All three model runs must exist before the first deadline and share one frozen as_of.",
        ],
    }
    text = json.dumps(output, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
