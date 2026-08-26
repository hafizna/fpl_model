"""Check freshness, fixture-completion, and FPL-finality for a set of model runs."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.release_freshness import (
    DEFAULT_STALE_AFTER_HOURS,
    check_release_freshness,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-run",
        dest="model_runs",
        action="append",
        required=True,
        help="A model_run_id to check. Repeat for each Gameweek in the horizon.",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--stale-after-hours",
        type=float,
        default=DEFAULT_STALE_AFTER_HOURS,
        help="Flag SNAPSHOT_STALE_RELATIVE_TO_NOW when the source snapshot is older than "
        "this and the Gameweek's deadline has not yet passed.",
    )
    parser.add_argument(
        "--now",
        type=lambda value: datetime.fromisoformat(value).astimezone(UTC),
        default=None,
        help="Override the reference time (ISO 8601). Defaults to the real current time; "
        "only intended for reproducing a past check.",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("outputs/release_freshness.json"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = check_release_freshness(
        model_run_ids=tuple(args.model_runs),
        database_path=args.database,
        now=args.now,
        stale_after_hours=args.stale_after_hours,
    )
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(result.report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"checked_at={result.report['checked_at']}")
    print(f"passes={result.passes}")
    if result.report["problems"]:
        print("problems:")
        for problem in result.report["problems"]:
            print(f"  - {problem}")
    for gw in result.report["gameweeks"]:
        fixtures = gw["fixtures"]
        finality = gw["fpl_finality"]
        print(
            f"GW{gw['target_gameweek']}: fixtures={fixtures['finished']}/{fixtures['total']} "
            f"analytically_complete={fixtures['analytically_complete']} "
            f"is_final={finality['is_final']} "
            f"drift_check_eligible={gw['drift_check_eligible']} "
            f"flags={gw['flags']}"
        )
    print(f"json={args.json_output.resolve()}")

    if not result.passes:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
