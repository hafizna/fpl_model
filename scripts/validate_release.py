"""Run the manifest, freshness, and approval gates against one named release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.release_freshness import DEFAULT_STALE_AFTER_HOURS
from fpl_model.validation.release_health import determine_release_health
from fpl_model.validation.release_orchestration import orchestrate_release_validation


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-run",
        dest="model_runs",
        action="append",
        required=True,
        help="A model_run_id to validate, ordered ascending by target Gameweek. "
        "Repeat for each Gameweek in the release (e.g. anchor GW plus GW+1/GW+2).",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--stale-after-hours",
        type=float,
        default=DEFAULT_STALE_AFTER_HOURS,
        help="Passed through to the freshness check.",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("outputs/release_validation.json"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = orchestrate_release_validation(
        model_run_ids=tuple(args.model_runs),
        database_path=args.database,
        stale_after_hours=args.stale_after_hours,
    )
    health = determine_release_health(orchestration_report=result.report)
    output = {**result.report, "health": health.report}
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"model_run_ids={result.report['model_run_ids']}")
    print(f"passes={result.passes}")
    print(f"approval_status={result.approval_status}")
    print(f"health.state={health.state} (label={health.label})")
    for reason in health.report["reasons"]:
        print(f"  health: {reason}")
    print(f"manifest.linkage.passes={result.report['manifest']['linkage']['passes']}")
    for problem in result.report["manifest"]["linkage"]["problems"]:
        print(f"  manifest: {problem}")
    print(f"freshness.passes={result.report['freshness']['passes']}")
    for problem in result.report["freshness"]["problems"]:
        print(f"  freshness: {problem}")
    print(f"approval.passes={result.report['approval']['passes']}")
    for problem in result.report["approval"]["problems"]:
        print(f"  approval: {problem}")
    print(f"json={args.json_output.resolve()}")

    if not result.passes:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
