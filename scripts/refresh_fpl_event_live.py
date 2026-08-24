"""Fetch and persist one official FPL event-live payload."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from fpl_model.ingest.fpl import FPLClient
from fpl_model.ingest.fpl_event_live import DEFAULT_RAW_ROOT, persist_fpl_event_live
from fpl_model.storage import DEFAULT_DATABASE_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gameweek", type=int, required=True)
    parser.add_argument("--source-ingestion-run", required=True)
    parser.add_argument("--season", default="2026-27")
    parser.add_argument("--allow-provisional", action="store_true")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    captured_at = datetime.now(UTC)
    result = persist_fpl_event_live(
        payload=FPLClient().event_live(args.gameweek),
        source_ingestion_run_id=args.source_ingestion_run,
        gameweek=args.gameweek,
        captured_at=captured_at,
        season=args.season,
        require_final=not args.allow_provisional,
        database_path=args.database,
        raw_root=args.raw_root,
    )
    print(
        f"Stored {result.live_run_id}: GW{result.gameweek} "
        f"players={result.player_rows} status={result.status}"
    )
    print(f"source={result.source_path}")


if __name__ == "__main__":
    main()
