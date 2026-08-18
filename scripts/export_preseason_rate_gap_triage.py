"""Export the ordered allowlist for manual preseason rate research."""

from __future__ import annotations

import argparse
from pathlib import Path

from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.gap_triage import export_preseason_rate_gap_triage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gameweek", type=int, default=1)
    parser.add_argument("--model-run")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/preseason_rate_gap_triage.csv"),
    )
    parser.add_argument("--show", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.show < 0:
        raise SystemExit("--show must be non-negative")
    frame = export_preseason_rate_gap_triage(
        args.output,
        database_path=args.database,
        model_run_id=args.model_run,
        target_gameweek=args.gameweek,
    )
    print(f"Exported {len(frame)} player-rate gaps to {args.output}")
    if args.show and not frame.empty:
        columns = [
            "research_rank",
            "player_name",
            "team",
            "position",
            "price",
            "expected_minutes",
            "rate_history_status",
        ]
        print(frame.loc[:, columns].head(args.show).to_string(index=False))


if __name__ == "__main__":
    main()
