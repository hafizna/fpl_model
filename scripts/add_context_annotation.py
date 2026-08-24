from __future__ import annotations

import argparse
import json
from datetime import datetime

from fpl_model.context.pipeline import (
    ReviewedContextAnnotation,
    store_reviewed_context_annotation,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Store one immutable reviewed manager/readiness/role annotation."
    )
    parser.add_argument("--subject-type", choices=("player", "team"), required=True)
    parser.add_argument(
        "--context-type",
        choices=("manager_regime", "readiness", "tactical_role"),
        required=True,
    )
    parser.add_argument("--player-code", type=int)
    parser.add_argument("--team-id", type=int)
    parser.add_argument("--observed-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--effective-from", type=datetime.fromisoformat, required=True)
    parser.add_argument("--effective-until", type=datetime.fromisoformat)
    parser.add_argument("--payload-json", type=json.loads, required=True)
    parser.add_argument("--source-reference", required=True)
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--database", default="data/processed/fpl_model.duckdb")
    args = parser.parse_args()
    annotation_id = store_reviewed_context_annotation(
        ReviewedContextAnnotation(
            subject_type=args.subject_type,
            context_type=args.context_type,
            observed_at=args.observed_at,
            effective_from=args.effective_from,
            effective_until=args.effective_until,
            payload=args.payload_json,
            source_reference=args.source_reference,
            rationale=args.rationale,
            player_code=args.player_code,
            team_id=args.team_id,
        ),
        database_path=args.database,
    )
    print(f"annotation_id={annotation_id}")


if __name__ == "__main__":
    main()
