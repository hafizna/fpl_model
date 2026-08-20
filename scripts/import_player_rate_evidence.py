"""Validate and store reviewed missing-player evidence without applying a rate prior."""

from __future__ import annotations

import argparse
from pathlib import Path

from fpl_model.ingest.player_rate_evidence import import_player_rate_evidence
from fpl_model.storage import DEFAULT_DATABASE_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--source-ingestion-run", required=True)
    parser.add_argument("--target-gameweek", type=int, default=1)
    parser.add_argument("--source-label", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = import_player_rate_evidence(
        args.csv,
        source_ingestion_run_id=args.source_ingestion_run,
        target_gameweek=args.target_gameweek,
        source_label=args.source_label,
        database_path=args.database,
    )
    print(
        f"Stored {result.evidence_import_run_id}: rows={result.evidence_rows} "
        f"sha256={result.source_sha256}"
    )
    print("Production player_rate_history and xPts were not modified.")


if __name__ == "__main__":
    main()
