"""Run a deadline-safe historical pipeline smoke test against Vaastav outcomes."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict

from fpl_model.ingest.vaastav import RAW_BASE, VaastavClient
from fpl_model.validation.backtest import score_predictions, walk_forward_folds
from fpl_model.validation.historical import materialize_expanding_player_mean_baseline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", default="2025-26")
    parser.add_argument(
        "--evaluation-from-gw",
        type=int,
        default=2,
        help="Exclude the all-cold-start first GW by default",
    )
    return parser.parse_args()


def run(*, season: str, evaluation_from_gw: int) -> dict[str, object]:
    if not 1 <= evaluation_from_gw <= 38:
        raise ValueError("evaluation_from_gw must be between 1 and 38")
    gameweeks = VaastavClient().merged_gameweeks(season)
    exact_duplicates = int(gameweeks.duplicated().sum())
    materialized_sha256 = hashlib.sha256(
        gameweeks.to_csv(index=False, lineterminator="\n").encode("utf-8")
    ).hexdigest()
    observations = materialize_expanding_player_mean_baseline(
        gameweeks,
        season=season,
    )
    evaluation = tuple(
        row for row in observations if row.gameweek >= evaluation_from_gw
    )
    metrics = score_predictions(evaluation)
    folds = walk_forward_folds(observations)
    return {
        "label": "expanding_player_mean_pipeline_smoke_test",
        "is_benchwarmers_baseline": False,
        "season": season,
        "source": f"{RAW_BASE}/{season}/gws/merged_gw.csv",
        "materialized_frame_sha256": materialized_sha256,
        "deadline_method": "earliest GW kickoff minus 90 minutes (inferred, not archived)",
        "outcome_available_method": "kickoff plus 3 hours",
        "prediction": "mean actual points from outcomes available before the deadline; cold start 0",
        "all_rows": len(observations),
        "source_rows": len(gameweeks),
        "exact_duplicate_rows_removed": exact_duplicates,
        "evaluation_from_gw": evaluation_from_gw,
        "evaluation_rows": len(evaluation),
        "evaluation_population": (
            "all player-fixture rows; zero-minute outcomes retained to avoid lookahead filtering"
        ),
        "walk_forward_folds": len(folds),
        "metrics": asdict(metrics),
        "limitations": [
            "This validates the pipeline, not the reproduced Benchwarmers model.",
            "Deadlines are inferred because merged_gw.csv has no archived deadline field.",
            "A real baseline benchmark needs deadline-time model inputs and fixture snapshots.",
            "Metrics cover the full selectable player population, so zero-point rows are common.",
        ],
    }


def main() -> None:
    args = parse_args()
    print(
        json.dumps(
            run(
                season=args.season,
                evaluation_from_gw=args.evaluation_from_gw,
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
