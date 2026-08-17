"""Audit deadline-time Vaastav snapshot coverage from repository history."""

from __future__ import annotations

import argparse
import json
from statistics import median

from fpl_model.ingest.vaastav import VaastavClient, latest_revision_at_or_before
from fpl_model.validation.historical import infer_gameweek_deadlines


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--fresh-days", type=float, default=14.0)
    return parser.parse_args()


def run(*, season: str, fresh_days: float) -> dict[str, object]:
    if fresh_days <= 0.0:
        raise ValueError("fresh_days must be positive")
    client = VaastavClient()
    gameweeks = client.merged_gameweeks(season)
    deadlines = infer_gameweek_deadlines(gameweeks)
    revisions = client.file_revisions(season)

    coverage: list[dict[str, object]] = []
    ages: list[float] = []
    for gameweek, deadline in deadlines.items():
        revision = latest_revision_at_or_before(
            revisions,
            deadline.to_pydatetime(),
        )
        if revision is None:
            coverage.append(
                {
                    "gameweek": gameweek,
                    "deadline": deadline.isoformat(),
                    "revision": None,
                    "age_days": None,
                }
            )
            continue
        age_days = (deadline.to_pydatetime() - revision.committed_at).total_seconds() / 86_400
        ages.append(age_days)
        coverage.append(
            {
                "gameweek": gameweek,
                "deadline": deadline.isoformat(),
                "revision": revision.sha,
                "committed_at": revision.committed_at.isoformat(),
                "age_days": age_days,
                "fresh": age_days <= fresh_days,
            }
        )

    covered = [row for row in coverage if row["revision"] is not None]
    return {
        "label": "vaastav_deadline_snapshot_coverage_audit",
        "season": season,
        "snapshot_file": f"data/{season}/players_raw.csv",
        "selection_rule": "latest commit touching snapshot_file at or before inferred deadline",
        "deadline_method": "earliest GW kickoff minus 90 minutes (inferred, not archived)",
        "fresh_threshold_days": fresh_days,
        "revisions_found": len(revisions),
        "gameweeks": len(coverage),
        "gameweeks_with_snapshot": len(covered),
        "gameweeks_without_snapshot": len(coverage) - len(covered),
        "unique_revisions_used": len({row["revision"] for row in covered}),
        "fresh_gameweeks": sum(bool(row.get("fresh")) for row in covered),
        "median_snapshot_age_days": median(ages) if ages else None,
        "maximum_snapshot_age_days": max(ages) if ages else None,
        "coverage": coverage,
        "limitations": [
            "Commit time proves the snapshot existed by the cutoff, not that it was captured exactly at the FPL deadline.",
            "A stale snapshot is causal but may omit recent availability/injury changes.",
            "The inferred deadline is not an archived official deadline.",
        ],
    }


def main() -> None:
    args = parse_args()
    print(json.dumps(run(season=args.season, fresh_days=args.fresh_days), indent=2))


if __name__ == "__main__":
    main()
