"""Fit a shadow uncertainty artifact from the pinned 2025/26 walk-forward backtest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
from backtest_benchwarmers import _archive_and_import

from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation import benchwarmers_backtest
from fpl_model.validation.projection_uncertainty import (
    build_residual_rows,
    evaluate_intervals,
    store_uncertainty_artifact,
    walk_forward_intervals,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--revision")
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw/vaastav"))
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--minimum-segment-rows", type=int, default=100)
    parser.add_argument("--minimum-segment-gameweeks", type=int, default=5)
    parser.add_argument("--interval-mass", type=float, default=0.80)
    parser.add_argument(
        "--report-output",
        type=Path,
        default=Path(
            "docs/research/walk_forward_benchwarmers_2025_26_projection_uncertainty.json"
        ),
    )
    args = parser.parse_args()
    imported, players, gameweeks = _archive_and_import(
        season=args.season,
        revision_sha=args.revision,
        raw_root=args.raw_root,
        database_path=args.database,
    )
    gameweeks = gameweeks.merge(
        players.loc[:, ["id", "code"]], left_on="element", right_on="id", how="left"
    )
    with duckdb.connect(str(args.database)) as connection:
        backtest = benchwarmers_backtest.materialize_benchwarmers_walk_forward_backtest(
            season=args.season,
            import_run_id=imported.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks,
            players_raw_frame=players,
        )
    residuals = build_residual_rows(backtest.observations, backtest.diagnostics)
    artifact = store_uncertainty_artifact(
        residuals,
        source_season=args.season,
        source_model_version=benchwarmers_backtest.POLICY_VERSION,
        source_reference=f"vaastav_import={imported.import_run_id}",
        interval_mass=args.interval_mass,
        minimum_segment_rows=args.minimum_segment_rows,
        minimum_segment_gameweeks=args.minimum_segment_gameweeks,
        status="shadow",
        database_path=args.database,
    )
    intervals = walk_forward_intervals(
        residuals,
        interval_mass=args.interval_mass,
        minimum_segment_rows=args.minimum_segment_rows,
        minimum_segment_gameweeks=args.minimum_segment_gameweeks,
    )
    residual_by_key = {
        (row.player_code, row.fixture_id, row.gameweek): row for row in residuals
    }
    evaluation_rows = []
    for row in intervals:
        residual = residual_by_key[(row.player_code, row.fixture_id, row.gameweek)]
        for cohort in ("all", f"position_{residual.position}"):
            evaluation_rows.append(
                (
                    cohort,
                    row.actual_points,
                    row.lower_xpts,
                    row.upper_xpts,
                    row.predictive_rmse,
                )
            )
    evaluations = evaluate_intervals(tuple(evaluation_rows))
    report = {
        "$schema_note": (
            "Strictly prior-outcome walk-forward validation of residual-based xPts intervals. "
            "The resulting artifact remains shadow-only."
        ),
        "season": args.season,
        "source_model_version": benchwarmers_backtest.POLICY_VERSION,
        "artifact_id": artifact.artifact_id,
        "artifact_status": artifact.status,
        "interval_mass": args.interval_mass,
        "minimum_segment_rows": args.minimum_segment_rows,
        "minimum_segment_gameweeks": args.minimum_segment_gameweeks,
        "walk_forward_rows": len(intervals),
        "candidate_residual_rows": len(residuals),
        "risk_rmse_thresholds": {
            "low_max": artifact.low_risk_rmse_threshold,
            "medium_max": artifact.high_risk_rmse_threshold,
        },
        "cohorts": {
            row.cohort: {
                "observations": row.observations,
                "empirical_coverage": row.empirical_coverage,
                "mean_interval_width": row.mean_interval_width,
                "mean_predictive_rmse": row.mean_predictive_rmse,
            }
            for row in evaluations
        },
        "limitations": [
            "Intervals quantify historical residual dispersion, not parameter or model-selection uncertainty.",
            "Residual dependence between players and fixtures is not used by the existing lineup variance sum.",
            "Premium, cheap-enabler, promoted-team, new-signing, and role-change cohorts require prospectively captured production metadata and remain unvalidated.",
            "No shadow or active artifact changes mean xPts; only an explicitly approved artifact may populate the scalar uncertainty field.",
        ],
        "reproduce": (
            ".venv\\Scripts\\python.exe scripts/build_uncertainty_artifact.py "
            f"--season {args.season}"
        ),
    }
    args.report_output.parent.mkdir(parents=True, exist_ok=True)
    args.report_output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"artifact_id={artifact.artifact_id}")
    print(f"segments={artifact.segment_rows} status={artifact.status}")
    print(
        f"risk_rmse_thresholds={artifact.low_risk_rmse_threshold:.4f},"
        f"{artifact.high_risk_rmse_threshold:.4f}"
    )
    print(f"walk_forward_rows={len(intervals)} report={args.report_output}")


if __name__ == "__main__":
    main()
