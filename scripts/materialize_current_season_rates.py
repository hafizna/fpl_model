"""Materialise a deadline-safe, shrunk current-season player-rate update."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from fpl_model.model.current_season_rates import materialize_current_season_rates
from fpl_model.storage import DEFAULT_DATABASE_PATH


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset or Z")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-ingestion-run-id", required=True)
    parser.add_argument("--season", required=True)
    parser.add_argument(
        "--as-of-gameweek",
        type=int,
        required=True,
        help="Only Gameweeks strictly before this one are eligible for the rate window.",
    )
    parser.add_argument("--as-of", type=_timestamp, required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = materialize_current_season_rates(
        source_ingestion_run_id=args.source_ingestion_run_id,
        season=args.season,
        as_of_gameweek=args.as_of_gameweek,
        as_of=args.as_of,
        database_path=args.database,
    )
    print(f"rate_run_id={result.rate_run_id}")
    print(f"final_gameweeks={list(result.final_gameweeks)}")
    print(f"player_rows={result.player_rows}")
    print(f"status={result.status}")
    if not result.final_gameweeks:
        print(
            "WARNING: no final Gameweek was available before "
            f"GW{args.as_of_gameweek} as of {args.as_of.isoformat()} -- this run is empty."
        )


if __name__ == "__main__":
    main()
