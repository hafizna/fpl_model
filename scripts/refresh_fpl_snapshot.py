"""Fetch and persist one timestamped official FPL API snapshot."""

from __future__ import annotations

import argparse
from pathlib import Path

from fpl_model.ingest.fpl import FPLClient
from fpl_model.ingest.fpl_snapshot import DEFAULT_RAW_ROOT, persist_fpl_snapshot
from fpl_model.storage import DEFAULT_DATABASE_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", default="2026-27")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    bootstrap, fixtures, captured_at = FPLClient().snapshot_payload()
    result = persist_fpl_snapshot(
        bootstrap=bootstrap,
        fixtures=fixtures,
        captured_at=captured_at,
        season=args.season,
        database_path=args.database,
        raw_root=args.raw_root,
    )
    print(f"Stored {result.ingestion_run_id}")
    print(
        f"players={result.players} teams={result.teams} "
        f"gameweeks={result.gameweeks} fixtures={result.fixtures}"
    )
    print(f"manifest={result.manifest_path}")


if __name__ == "__main__":
    main()
