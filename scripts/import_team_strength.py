"""Import reviewed preseason team rates and derive fixture-strength signals."""

from __future__ import annotations

import argparse
from pathlib import Path

from fpl_model.ingest.team_strength import (
    import_team_strength_history,
    materialize_preseason_team_strength,
)
from fpl_model.storage import DEFAULT_DATABASE_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--target-season", required=True)
    parser.add_argument("--previous-season", required=True)
    parser.add_argument("--source-label", required=True)
    parser.add_argument("--gameweek", type=int, default=1)
    parser.add_argument("--source-ingestion-run")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    imported = import_team_strength_history(
        args.csv,
        target_season=args.target_season,
        previous_season=args.previous_season,
        source_label=args.source_label,
        database_path=args.database,
    )
    materialized = materialize_preseason_team_strength(
        source_import_run_id=imported.import_run_id,
        target_gameweek=args.gameweek,
        source_ingestion_run_id=args.source_ingestion_run,
        database_path=args.database,
    )
    print(
        f"Stored {imported.import_run_id}: teams={imported.team_rows} "
        f"sha256={imported.source_sha256}"
    )
    print(
        f"Materialised {materialized.strength_run_id}: "
        f"teams={materialized.team_rows} "
        f"FPL snapshot={materialized.source_ingestion_run_id}"
    )


if __name__ == "__main__":
    main()
