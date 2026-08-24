from __future__ import annotations

import argparse
import json
from pathlib import Path

from fpl_model.model.shadow_calibration import (
    materialize_shadow_calibration,
    store_shadow_calibration_artifact,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Store a committed historical xPts fit and apply it in shadow mode only."
    )
    parser.add_argument("--model-run-id", required=True)
    parser.add_argument(
        "--calibration-json",
        type=Path,
        default=Path("docs/research/walk_forward_benchwarmers_2025_26_calibration.json"),
    )
    parser.add_argument("--database", default="data/processed/fpl_model.duckdb")
    args = parser.parse_args()
    payload = json.loads(args.calibration_json.read_text(encoding="utf-8"))
    overall = payload["pooled_overall"]
    artifact = store_shadow_calibration_artifact(
        source_season=str(payload["season"]),
        source_model_version="benchwarmers_replica_walk_forward_backtest_v1",
        source_reference=str(args.calibration_json.resolve()),
        training_rows=int(overall["evaluation_rows"]),
        training_gameweeks=int(overall["eligible_gameweeks"]),
        slope=float(overall["pooled_ols_diagnostic_slope"]),
        intercept=float(overall["pooled_ols_diagnostic_intercept"]),
        status="shadow",
        database_path=args.database,
    )
    result = materialize_shadow_calibration(
        model_run_id=args.model_run_id,
        artifact_id=artifact.artifact_id,
        database_path=args.database,
    )
    print(f"artifact_id={result.artifact_id}")
    print(f"model_run_id={result.model_run_id}")
    print(f"rows={result.player_fixture_rows} mode=shadow")


if __name__ == "__main__":
    main()
