"""Append a reviewed start/cameo scenario for one player and gameweek."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from fpl_model.context.minutes import (
    create_appearance_scenario_override,
    store_appearance_scenario_override,
)
from fpl_model.model.appearance import ConditionalAppearanceScenario
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
    parser.add_argument("--start-if-available", type=float, required=True)
    parser.add_argument("--sub-if-available", type=float, required=True)
    parser.add_argument("--sixty-given-start", type=float, required=True)
    parser.add_argument("--minutes-per-start", type=float, required=True)
    parser.add_argument("--minutes-per-substitute", type=float, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    scenario = ConditionalAppearanceScenario(
        start_probability_if_available=args.start_if_available,
        substitute_probability_if_available=args.sub_if_available,
        sixty_probability_given_start=args.sixty_given_start,
        minutes_per_start=args.minutes_per_start,
        minutes_per_substitute=args.minutes_per_substitute,
    )
    override = create_appearance_scenario_override(
        player_code=args.player_code,
        target_gameweek=args.gameweek,
        observed_at=args.observed_at,
        effective_until=args.effective_until,
        scenario=scenario,
        source=args.source,
        rationale=args.rationale,
    )
    result = store_appearance_scenario_override(override, database_path=args.database)
    print(f"Stored {result.override_id}")
    if result.requires_pipeline_refresh:
        print(
            "Refresh FPL, resolve availability, then rerun appearance projection "
            "so the existing projection remains immutable."
        )


if __name__ == "__main__":
    main()
