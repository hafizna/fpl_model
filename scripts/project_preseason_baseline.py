"""Materialise the explainable GW1 Benchwarmers-compatible baseline."""

from __future__ import annotations

import argparse
from pathlib import Path

from fpl_model.model.baseline_pipeline import materialize_preseason_baseline
from fpl_model.storage import DEFAULT_DATABASE_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gameweek", type=int, default=1)
    parser.add_argument("--appearance-run")
    parser.add_argument("--player-rate-run")
    parser.add_argument("--team-strength-run")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = materialize_preseason_baseline(
        target_gameweek=args.gameweek,
        appearance_projection_run_id=args.appearance_run,
        player_rate_run_id=args.player_rate_run,
        team_strength_run_id=args.team_strength_run,
        database_path=args.database,
    )
    print(f"Stored {result.model_run_id} ({result.status})")
    print(
        f"players={result.current_players} "
        f"candidate_fixtures={result.candidate_fixture_rows} "
        f"projected={result.projected_fixture_rows} gaps={result.gap_players}"
    )


if __name__ == "__main__":
    main()
