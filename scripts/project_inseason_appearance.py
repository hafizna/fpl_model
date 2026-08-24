"""Materialise deadline-safe in-season appearance projections."""

from __future__ import annotations

import argparse
from pathlib import Path

from fpl_model.model.appearance_pipeline import materialize_inseason_appearance
from fpl_model.storage import DEFAULT_DATABASE_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gameweek", type=int, required=True)
    parser.add_argument("--current-season", default="2026-27")
    parser.add_argument("--previous-season", default="2025-26")
    parser.add_argument("--availability-run")
    parser.add_argument("--previous-effective-fixtures", type=float, default=5.0)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = materialize_inseason_appearance(
        target_gameweek=args.gameweek,
        current_season=args.current_season,
        previous_season=args.previous_season,
        availability_resolution_run_id=args.availability_run,
        previous_effective_fixtures=args.previous_effective_fixtures,
        database_path=args.database,
    )
    print(f"Stored {result.projection_run_id} ({result.status})")
    print(
        f"players={result.players} projected={result.projected_players} "
        f"missing={result.missing_players}"
    )


if __name__ == "__main__":
    main()
