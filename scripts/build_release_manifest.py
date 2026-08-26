"""Link one Gameweek horizon's model runs into one immutable release manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.release_manifest import build_release_manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-run",
        dest="model_runs",
        action="append",
        required=True,
        help="A model_run_id to include, ordered ascending by target gameweek. "
        "Repeat for each Gameweek in the horizon (e.g. anchor GW plus GW+1/GW+2).",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("outputs/release_manifest.json"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = build_release_manifest(
        model_run_ids=tuple(args.model_runs),
        database_path=args.database,
    )
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(manifest.report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    linkage = manifest.report["linkage"]
    shadow = manifest.report["shadow_status"]
    print(f"manifest_id={manifest.manifest_id}")
    print(f"model_run_ids={manifest.report['model_run_ids']}")
    print(f"linkage.passes={linkage['passes']}")
    if linkage["problems"]:
        print("linkage.problems:")
        for problem in linkage["problems"]:
            print(f"  - {problem}")
    if shadow["shadow_calibration_missing_for_gameweeks"]:
        print(
            "shadow calibration missing for GWs: "
            f"{shadow['shadow_calibration_missing_for_gameweeks']}"
        )
    if shadow["uncertainty_missing_for_gameweeks"]:
        print(f"uncertainty missing for GWs: {shadow['uncertainty_missing_for_gameweeks']}")
    if shadow["context_missing_for_gameweeks"]:
        print(f"context missing for GWs: {shadow['context_missing_for_gameweeks']}")
    if shadow["non_final_event_live_runs"]:
        print("non-final event-live evidence (analytically complete, not FPL-final):")
        for entry in shadow["non_final_event_live_runs"]:
            print(
                f"  - GW{entry['target_gameweek']} appearance input uses "
                f"{entry['live_run_id']} (event GW{entry['event_gameweek']}, "
                f"event_finished={entry['event_finished']}, "
                f"data_checked={entry['data_checked']})"
            )
    print(f"json={args.json_output.resolve()}")

    if not linkage["passes"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
