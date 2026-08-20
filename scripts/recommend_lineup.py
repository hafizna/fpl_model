"""Recommend a legal starting XI, bench order, captain, and vice-captain."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from fpl_model.decision.lineup import recommend_lineup
from fpl_model.decision.lineup_store import load_lineup_inputs
from fpl_model.storage import DEFAULT_DATABASE_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--squad-snapshot-id", required=True)
    parser.add_argument("--model-run-id", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _player(player, projection_by_id) -> dict[str, object]:
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
    }


def main() -> None:
    args = parse_args()
    with duckdb.connect(str(args.database), read_only=True) as connection:
        inputs = load_lineup_inputs(
            connection,
            squad_snapshot_id=args.squad_snapshot_id,
            model_run_id=args.model_run_id,
        )
    recommendation = recommend_lineup(inputs.squad, inputs.projections)
    projection_by_id = {projection.fpl_id: projection for projection in inputs.projections}
    result = {
        "squad_snapshot_id": inputs.squad_snapshot_id,
        "model_run_id": inputs.model_run_id,
        "target_gameweek": inputs.target_gameweek,
        "formation": recommendation.formation,
        "starting_xpts": recommendation.starting_xpts,
        "captain_bonus_xpts": recommendation.captain_bonus_xpts,
        "total_xpts": recommendation.total_xpts,
        "uncertainty": recommendation.uncertainty,
        "captain": _player(recommendation.captain, projection_by_id),
        "vice_captain": _player(recommendation.vice_captain, projection_by_id),
        "starters": [
            _player(player, projection_by_id) for player in recommendation.starters
        ],
        "bench_goalkeeper": _player(recommendation.bench_goalkeeper, projection_by_id),
        "outfield_bench_order": [
            _player(player, projection_by_id)
            for player in recommendation.outfield_bench_order
        ],
        "data_quality_flags": list(recommendation.data_quality_flags),
        "method_note": (
            "Exhaustive maximum-mean-xPts search across every legal XI; captain is the "
            "highest-xPts starter and vice-captain the second highest. Missing squad "
            "projections are rejected, never treated as zero."
        ),
    }
    output = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
