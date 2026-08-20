"""Import a manual, deadline-safe FPL manager-squad snapshot into DuckDB."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from fpl_model.decision.squad import CHIP_NAMES, CHIP_STATUSES
from fpl_model.ingest.squad_snapshot import import_squad_snapshot_csv
from fpl_model.storage import DEFAULT_DATABASE_PATH


def _chip_state(value: str) -> tuple[str, str]:
    try:
        chip, status = (part.strip().lower() for part in value.split("=", 1))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("chip state must use CHIP=STATUS") from exc
    if chip not in CHIP_NAMES or status not in CHIP_STATUSES:
        raise argparse.ArgumentTypeError(
            f"chip state must use one of {CHIP_NAMES} and one of {CHIP_STATUSES}"
        )
    return chip, status


def _timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be ISO 8601") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone offset")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--entry-id", type=int, required=True)
    parser.add_argument("--entry-name")
    parser.add_argument("--season", default="2026-27")
    parser.add_argument("--gameweek", type=int, required=True)
    parser.add_argument("--source-ingestion-run-id", required=True)
    parser.add_argument("--captured-at", type=_timestamp, required=True)
    parser.add_argument("--source-label", required=True)
    parser.add_argument("--bank", required=True, help="Money in the bank, e.g. 0.5")
    transfer_group = parser.add_mutually_exclusive_group(required=True)
    transfer_group.add_argument("--free-transfers", type=int)
    transfer_group.add_argument("--unlimited-transfers", action="store_true")
    parser.add_argument("--chip-period", type=int, choices=(1, 2), required=True)
    parser.add_argument(
        "--chip-state",
        action="append",
        type=_chip_state,
        required=True,
        metavar="CHIP=STATUS",
        help=(
            "Repeat once for wildcard, free_hit, bench_boost, and triple_captain; "
            "status is available, active, played, or expired"
        ),
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    chip_states = dict(args.chip_state)
    if len(chip_states) != len(args.chip_state):
        raise ValueError("each chip may be specified only once")
    result = import_squad_snapshot_csv(
        args.csv,
        entry_id=args.entry_id,
        entry_name=args.entry_name,
        season=args.season,
        target_gameweek=args.gameweek,
        source_ingestion_run_id=args.source_ingestion_run_id,
        captured_at=args.captured_at,
        source_label=args.source_label,
        bank=args.bank,
        free_transfers=args.free_transfers,
        unlimited_transfers=args.unlimited_transfers,
        chip_period=args.chip_period,
        chip_states=chip_states,
        database_path=args.database,
    )
    print(
        f"Stored {result.squad_snapshot_id}: entry={result.entry_id} "
        f"season={result.season} GW={result.target_gameweek} players={result.player_rows} "
        f"team_value={result.team_value_tenths / 10:.1f} sha256={result.source_sha256}"
    )


if __name__ == "__main__":
    main()
