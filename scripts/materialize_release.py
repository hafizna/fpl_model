"""Materialise and validate one complete in-season three-Gameweek release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.release_materialization import materialize_inseason_release


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gameweek", type=int, required=True)
    parser.add_argument("--current-season", default="2026-27")
    parser.add_argument("--previous-season", default="2025-26")
    parser.add_argument("--team-strength-csv", type=Path, required=True)
    parser.add_argument("--team-strength-source-label", required=True)
    parser.add_argument("--vaastav-players-csv", type=Path, required=True)
    parser.add_argument("--vaastav-source-revision", required=True)
    parser.add_argument("--calibration-artifact-id", required=True)
    parser.add_argument("--uncertainty-artifact-id", required=True)
    parser.add_argument("--previous-effective-fixtures", type=float, default=5.0)
    parser.add_argument("--allow-analytically-complete", action="store_true")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = materialize_inseason_release(
        target_gameweek=args.gameweek,
        current_season=args.current_season,
        previous_season=args.previous_season,
        team_strength_csv=args.team_strength_csv,
        team_strength_source_label=args.team_strength_source_label,
        vaastav_players_csv=args.vaastav_players_csv,
        vaastav_source_revision=args.vaastav_source_revision,
        calibration_artifact_id=args.calibration_artifact_id,
        uncertainty_artifact_id=args.uncertainty_artifact_id,
        previous_effective_fixtures=args.previous_effective_fixtures,
        allow_analytically_complete=args.allow_analytically_complete,
        database_path=args.database,
    )
    output = json.dumps(result.report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    if not result.passes:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
