"""Export a targeted CSV template for missing-player rate research."""

from __future__ import annotations

import argparse
from pathlib import Path

from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.gap_triage import export_player_rate_evidence_template


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gameweek", type=int, default=1)
    parser.add_argument("--model-run")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--exclude-team",
        action="append",
        default=[],
        help="Repeat to exclude a team abbreviation, for example COV",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/player_rate_evidence_template.csv"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    frame = export_player_rate_evidence_template(
        args.output,
        database_path=args.database,
        model_run_id=args.model_run,
        target_gameweek=args.gameweek,
        limit=args.limit,
        excluded_teams=tuple(args.exclude_team),
    )
    print(f"Exported {len(frame)} evidence rows to {args.output}")
    print("This template is research evidence only and does not change production xPts.")


if __name__ == "__main__":
    main()
