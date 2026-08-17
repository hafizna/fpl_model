"""Import a validated workbook appearance-history CSV into local DuckDB."""

from __future__ import annotations

import argparse
from pathlib import Path

from fpl_model.ingest.appearance_history import import_appearance_history_csv
from fpl_model.storage import DEFAULT_DATABASE_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--season", required=True)
    parser.add_argument("--source-label", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = import_appearance_history_csv(
        args.csv,
        season=args.season,
        source_label=args.source_label,
        database_path=args.database,
    )
    print(
        f"Stored {result.import_run_id}: season={result.season} "
        f"players={result.player_rows} sha256={result.source_sha256}"
    )


if __name__ == "__main__":
    main()
