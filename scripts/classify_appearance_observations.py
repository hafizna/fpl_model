"""Classify every player's realised outcome for one completed, final Gameweek."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import duckdb

from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.appearance_observation import (
    appearance_observation_report,
    load_appearance_observations,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live-run-id",
        required=True,
        help="A final (event_finished AND data_checked) fpl_event_live_run.",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("outputs/appearance_observations.json"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with duckdb.connect(str(args.database), read_only=True) as connection:
        observations = load_appearance_observations(connection, live_run_id=args.live_run_id)

    counts = Counter(result.observation for result in observations.values())
    report = {
        "label": "appearance_observation_v1",
        "live_run_id": args.live_run_id,
        "players": {
            str(fpl_id): appearance_observation_report(result)
            for fpl_id, result in sorted(observations.items())
        },
        "counts_by_observation": dict(sorted(counts.items())),
        "limitations": [
            "FPL's live-data endpoint does not expose the 20-man matchday squad or bench "
            "list, so an unused substitute cannot be told apart from a player left out of "
            "the squad entirely -- both collapse into the single named "
            "'unused_substitute_or_not_in_squad' bucket rather than being guessed.",
            "This is retrospective and read-only: it classifies what already happened in "
            "one FINAL Gameweek. It does not alter any projection, role state, or "
            "recommendation.",
        ],
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"live_run_id={args.live_run_id}")
    print(f"players_classified={len(observations)}")
    for observation, count in sorted(counts.items()):
        print(f"  {observation}={count}")
    print(f"json={args.json_output.resolve()}")


if __name__ == "__main__":
    main()
