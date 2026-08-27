"""Compare one model run's projections against its own final event-live outcome."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.material_conflict import (
    audit_material_conflicts,
    material_conflict_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-run-id", required=True)
    parser.add_argument(
        "--live-run-id",
        required=True,
        help="A final (event_finished AND data_checked) fpl_event_live_run for the SAME "
        "Gameweek and official snapshot the model run used.",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--json-output",
        type=Path,
        default=Path("outputs/material_conflicts.json"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    with duckdb.connect(str(args.database), read_only=True) as connection:
        conflicts = audit_material_conflicts(
            connection,
            model_run_id=args.model_run_id,
            live_run_id=args.live_run_id,
        )

    report = {
        "label": "material_conflict_audit_v1",
        "model_run_id": args.model_run_id,
        "live_run_id": args.live_run_id,
        "conflicts": material_conflict_report(conflicts),
        "limitations": [
            "This audits exactly the two named conflict shapes design principle 6's role-state "
            "work requires (a 60+ minute start with low projected start probability, or a "
            "zero-minute blank with high projected appearance probability); it does not score "
            "or rank every projection error, only these two specific, material shapes.",
            "This is retrospective only: it compares one already-completed model run against "
            "its own already-final outcome. It does not itself warn on a future decision -- "
            "that is a separate, prospective P0 item built on top of this audit.",
        ],
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"model_run_id={args.model_run_id}")
    print(f"live_run_id={args.live_run_id}")
    print(f"conflicts_found={len(conflicts)}")
    for row in conflicts:
        print(f"  fpl_id={row.fpl_id} type={row.conflict_type}: {row.reason}")
    print(f"json={args.json_output.resolve()}")


if __name__ == "__main__":
    main()
