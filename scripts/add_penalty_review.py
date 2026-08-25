from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

from fpl_model.ingest.penalty_review import (
    PlayerPenaltyEvent,
    store_event_penalty_review,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Store a complete reviewed penalty ledger for one final event-live run."
    )
    parser.add_argument("--live-run-id", required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--observed-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--source-reference", required=True)
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--database", default="data/processed/fpl_model.duckdb")
    args = parser.parse_args()
    frame = pd.read_csv(args.csv)
    required = {"fpl_id", "attempts", "goals", "penalty_xg"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"penalty CSV missing columns: {sorted(missing)}")
    events = tuple(
        PlayerPenaltyEvent(
            fpl_id=int(row.fpl_id),
            attempts=int(row.attempts),
            goals=int(row.goals),
            penalty_xg=float(row.penalty_xg),
        )
        for row in frame.itertuples(index=False)
    )
    result = store_event_penalty_review(
        live_run_id=args.live_run_id,
        observed_at=args.observed_at,
        source_reference=args.source_reference,
        rationale=args.rationale,
        penalty_events=events,
        database_path=args.database,
    )
    print(f"review_id={result.review_id}")
    print(f"players={result.player_rows} penalty_takers={result.penalty_takers}")


if __name__ == "__main__":
    main()
