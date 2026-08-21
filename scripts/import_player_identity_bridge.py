"""Build an immutable official-FPL/Vaastav player identity bridge."""

from __future__ import annotations

import argparse
from pathlib import Path

from fpl_model.ingest.player_identity import import_player_identity_bridge
from fpl_model.storage import DEFAULT_DATABASE_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vaastav-players-csv", type=Path, required=True)
    parser.add_argument("--source-ingestion-run-id", required=True)
    parser.add_argument("--target-season", required=True)
    parser.add_argument("--vaastav-season", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = import_player_identity_bridge(
        args.vaastav_players_csv,
        source_ingestion_run_id=args.source_ingestion_run_id,
        target_season=args.target_season,
        vaastav_season=args.vaastav_season,
        source_revision=args.source_revision,
        database_path=args.database,
    )
    print(
        f"Stored {result.bridge_run_id}: matched={result.matched_players} "
        f"official_only={result.official_only_players} "
        f"vaastav_only={result.vaastav_only_players} "
        f"name_mismatches={result.name_mismatch_players} status={result.status}"
    )


if __name__ == "__main__":
    main()
