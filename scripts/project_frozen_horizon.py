"""Project an anchor baseline's fixtures across a frozen three-Gameweek horizon."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fpl_model.model.baseline_pipeline import materialize_frozen_projection_horizon
from fpl_model.storage import DEFAULT_DATABASE_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchor-model-run-id", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = materialize_frozen_projection_horizon(
        anchor_model_run_id=args.anchor_model_run_id,
        database_path=args.database,
    )
    print(
        json.dumps(
            {
                "anchor_model_run_id": result.anchor_model_run_id,
                "horizon_policy_version": result.horizon_policy_version,
                "start_gameweek": result.start_gameweek,
                "end_gameweek": result.end_gameweek,
                "model_run_ids": list(result.model_run_ids),
                "runs": [
                    {
                        "model_run_id": run.model_run_id,
                        "projected_fixture_rows": run.projected_fixture_rows,
                        "gap_players": run.gap_players,
                        "status": run.status,
                    }
                    for run in result.runs
                ],
                "method_note": (
                    "Each fixture Gameweek is rescored against its own opponent and venue. "
                    "Appearance, player-rate, and team-strength inputs remain frozen to the "
                    "anchor as_of and future rows carry an explicit frozen-input quality flag."
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
