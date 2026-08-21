"""Classify every gap in one immutable baseline projection run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.projection_coverage import audit_projection_coverage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-run")
    parser.add_argument("--gameweek", type=int, default=1)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("outputs/projection_coverage_audit.json"),
    )
    parser.add_argument(
        "--csv-output",
        type=Path,
        default=Path("outputs/projection_coverage_gaps.csv"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    audit = audit_projection_coverage(
        database_path=args.database,
        model_run_id=args.model_run,
        target_gameweek=args.gameweek,
    )
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.csv_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(audit.report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    csv_frame = audit.gaps.copy()
    csv_frame["data_quality_flags"] = csv_frame["data_quality_flags"].map(json.dumps)
    csv_frame.to_csv(args.csv_output, index=False, encoding="utf-8")

    coverage = audit.report["coverage"]
    print(
        f"Audited {coverage['gap_players']} gaps for {audit.report['model_run_id']}: "
        f"selectable coverage={coverage['selectable_coverage']:.1%} "
        f"target={coverage['target_selectable_coverage']:.0%} "
        f"passes={coverage['passes_target']}"
    )
    print(json.dumps(audit.report["summaries"]["primary_reason"], indent=2))
    print(f"json={args.json_output.resolve()}")
    print(f"csv={args.csv_output.resolve()}")


if __name__ == "__main__":
    main()
