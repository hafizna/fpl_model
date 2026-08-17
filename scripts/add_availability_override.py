"""Append one reviewed, deadline-safe player availability override."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from fpl_model.context.availability import (
    create_reviewed_override,
    store_reviewed_override,
)
from fpl_model.storage import DEFAULT_DATABASE_PATH


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a UTC offset or Z")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--player-code", type=int, required=True)
    parser.add_argument("--gameweek", type=int, required=True)
    parser.add_argument("--observed-at", type=_timestamp, required=True)
    parser.add_argument("--effective-until", type=_timestamp)
    parser.add_argument("--probability", type=float)
    eligibility = parser.add_mutually_exclusive_group()
    eligibility.add_argument("--eligible", action="store_true")
    eligibility.add_argument("--ineligible", action="store_true")
    parser.add_argument("--source", required=True)
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    args = parser.parse_args()
    if args.probability is None and not args.eligible and not args.ineligible:
        parser.error("set --probability, --eligible, or --ineligible")
    return args


def main() -> None:
    args = parse_args()
    is_eligible = True if args.eligible else False if args.ineligible else None
    override = create_reviewed_override(
        player_code=args.player_code,
        target_gameweek=args.gameweek,
        observed_at=args.observed_at,
        effective_until=args.effective_until,
        availability_probability=args.probability,
        is_eligible=is_eligible,
        source=args.source,
        rationale=args.rationale,
    )
    result = store_reviewed_override(override, database_path=args.database)
    print(f"Stored {result.override_id}")
    if result.requires_fpl_refresh:
        print(
            "Refresh the official FPL snapshot, then rerun availability resolution "
            "so this immutable override is included in a new resolution run."
        )


if __name__ == "__main__":
    main()
