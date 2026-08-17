"""Materialise GW1 appearance/start/minutes projections."""

from __future__ import annotations

import argparse
from pathlib import Path

from fpl_model.model.appearance_pipeline import materialize_preseason_appearance
from fpl_model.storage import DEFAULT_DATABASE_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gameweek", type=int, default=1)
    parser.add_argument("--previous-season", default="2025-26")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = materialize_preseason_appearance(
        target_gameweek=args.gameweek,
        previous_season=args.previous_season,
        database_path=args.database,
    )
    print(f"Stored {result.projection_run_id} ({result.status})")
    print(
        f"players={result.players} projected={result.projected_players} "
        f"missing={result.missing_players}"
    )


if __name__ == "__main__":
    main()
