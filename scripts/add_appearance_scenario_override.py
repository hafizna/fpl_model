"""Append a reviewed start/cameo scenario for one player and gameweek."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from fpl_model.context.appearance_scenario_presets import (
    APPEARANCE_SCENARIO_PRESETS,
    apply_appearance_scenario_preset,
)
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
    parser.add_argument(
        "--preset",
        choices=APPEARANCE_SCENARIO_PRESETS,
        help="Start from a named preset (likely_starter, rotation_risk, likely_bench) instead "
        "of specifying every scenario field by hand. Individual --start-if-available/etc. "
        "flags below still override one field of the chosen preset when both are given.",
    )
    parser.add_argument("--start-if-available", type=float)
    parser.add_argument("--sub-if-available", type=float)
    parser.add_argument("--sixty-given-start", type=float)
    parser.add_argument("--minutes-per-start", type=float)
    parser.add_argument("--minutes-per-substitute", type=float)
    parser.add_argument("--source", required=True)
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    args = parser.parse_args()
    manual_fields = (
        args.start_if_available,
        args.sub_if_available,
        args.sixty_given_start,
        args.minutes_per_start,
        args.minutes_per_substitute,
    )
    if args.preset is None and any(field is None for field in manual_fields):
        parser.error(
            "either --preset or all of --start-if-available/--sub-if-available/"
            "--sixty-given-start/--minutes-per-start/--minutes-per-substitute is required"
        )
    return args


def _build_scenario(args: argparse.Namespace) -> ConditionalAppearanceScenario:
    overrides = {
        name: value
        for name, value in (
            ("start_probability_if_available", args.start_if_available),
            ("substitute_probability_if_available", args.sub_if_available),
            ("sixty_probability_given_start", args.sixty_given_start),
            ("minutes_per_start", args.minutes_per_start),
            ("minutes_per_substitute", args.minutes_per_substitute),
        )
        if value is not None
    }
    if args.preset is not None:
        return apply_appearance_scenario_preset(args.preset, **overrides)
    return ConditionalAppearanceScenario(**overrides)


def main() -> None:
    args = parse_args()
    scenario = _build_scenario(args)
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
    print(f"Effective until: {result.effective_until.isoformat()}")
    if result.requires_pipeline_refresh:
        print(
            "Refresh FPL, resolve availability, then rerun appearance projection "
            "so the existing projection remains immutable."
        )


if __name__ == "__main__":
    main()
