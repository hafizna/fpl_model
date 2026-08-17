"""Materialise deadline-safe FPL availability for one target gameweek."""

from __future__ import annotations

import argparse
from pathlib import Path

from fpl_model.context.availability import materialize_latest_fpl_availability
from fpl_model.storage import DEFAULT_DATABASE_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gameweek", type=int, required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = materialize_latest_fpl_availability(
        target_gameweek=args.gameweek,
        database_path=args.database,
    )
    print(f"Stored {result.resolution_run_id} ({result.status})")
    print(
        f"players={result.players} resolved={result.resolved_players} "
        f"unresolved={result.unresolved_players} blocked={result.blocked_players}"
    )
    print(f"as_of={result.as_of.isoformat()} deadline={result.deadline.isoformat()}")


if __name__ == "__main__":
    main()
