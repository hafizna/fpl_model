"""Check that every released projection row has APPROVED calibration/uncertainty."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.release_approval import check_release_approval


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
        "--json-output",
        type=Path,
        default=Path("outputs/release_approval.json"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = check_release_approval(
        model_run_ids=tuple(args.model_runs),
        database_path=args.database,
    )
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(result.report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"passes={result.passes}")
    if result.report["problems"]:
        print("problems:")
        for problem in result.report["problems"]:
            print(f"  - {problem}")
    print(f"json={args.json_output.resolve()}")

    if not result.passes:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
