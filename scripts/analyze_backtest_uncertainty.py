"""Quantify paired uncertainty of the model's MAE/RMSE advantage over matched-naive.

This is a read-only measurement over already-scored observations from
``materialize_benchwarmers_walk_forward_backtest`` and
``matched_naive_observations`` (the same functions
``scripts/backtest_benchwarmers.py`` calls) -- it never calls any
``project_benchwarmers_*``/``weight_*`` component function differently, never
introduces a second scoring path, and does not change any formula,
calibration, or shrinkage. Its purpose is to answer whether the model's known
MAE 0.0190 / RMSE 0.0639 advantage over the matched-naive comparator is a
stable signal across the season's 36 gameweeks, or noise.

Uncertainty is estimated with a gameweek-cluster percentile bootstrap
(``fpl_model.validation.paired_uncertainty``): rows are not independent, since
every row scored at a given gameweek's deadline shares that gameweek's
causally-derived team-strength/rate/appearance inputs, so resampling must
resample whole gameweeks, not individual rows. A fixture-clustered bootstrap
is also reported as a labelled sensitivity check, never as the primary
result.

Before writing any output, this run's recomputed aggregate metrics are
compared against ``--reference`` (defaulting to the committed
``docs/research/walk_forward_benchwarmers_2025_26.json``) via
``validation.backtest_self_check.verify_self_check``, and this run's paired
point estimates are additionally checked against that reference's own
``absolute_mae_improvement``/``absolute_rmse_improvement`` fields. A mismatch
in either check raises ``ValueError`` and nothing is written.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import pandas as pd

from fpl_model.ingest.player_history import import_player_fixture_history
from fpl_model.ingest.vaastav import VaastavClient
from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.backtest import score_predictions
from fpl_model.validation.backtest_self_check import load_reference, verify_self_check
from fpl_model.validation.benchwarmers_backtest import (
    materialize_benchwarmers_walk_forward_backtest,
)
from fpl_model.validation.matched_naive import matched_naive_observations
from fpl_model.validation.paired_uncertainty import (
    DEFAULT_RESAMPLES,
    DEFAULT_SEED,
    build_method_notes,
    estimate_paired_uncertainty,
    interpret_paired_verdict,
)

# Machine-noise allowance only (see backtest_self_check.FLOAT_METRIC_TOLERANCE
# for the identical rationale) -- the point estimates recomputed here should
# be exactly the reference's absolute_mae_improvement/absolute_rmse_improvement
# since both are the same pooled-difference quantity, just derived through a
# different code path (paired rows vs two independently scored BacktestMetrics).
POINT_ESTIMATE_TOLERANCE = 1e-9

LIMITATIONS = [
    "Bootstrap intervals describe uncertainty under this specific historical "
    "resampling scheme (gameweek-cluster percentile bootstrap over the "
    "2025-26 archive); they are not a guarantee about future seasons or "
    "other leagues.",
    "36 evaluated gameweeks is a small number of primary bootstrap clusters "
    "-- the percentile CI's own precision is itself limited by this count, "
    "not just by the resample count (which only approximates the true "
    "36-cluster sampling distribution, it cannot exceed its information).",
    "This is one season (2025-26) only; no cross-season validation exists yet.",
    "An interval that excludes zero supports the model's advantage being "
    "stable within this sample; it is not proof of universal superiority "
    "over the matched-naive comparator in any other season or context.",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--revision", help="Exact Git revision of merged_gw.csv to pin")
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw/vaastav"))
    parser.add_argument("--evaluation-from-gw", type=int, default=3)
    parser.add_argument("--evaluation-to-gw", type=int, default=38)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--short-form-gameweeks", type=int, default=6)
    parser.add_argument("--defcon-short-form-gameweeks", type=int, default=10)
    parser.add_argument("--long-form-weight", type=float, default=0.8)
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/research/walk_forward_benchwarmers_2025_26_uncertainty.json"),
    )
    parser.add_argument(
        "--reference",
        type=Path,
        default=Path("docs/research/walk_forward_benchwarmers_2025_26.json"),
        help=(
            "Production backtest JSON this run's aggregate metrics and paired "
            "point estimates must match before any output is written."
        ),
    )
    return parser.parse_args()


def _archive_and_import(
    *,
    season: str,
    revision_sha: str | None,
    raw_root: Path,
    database_path: Path,
):
    """Pin the newest (or requested) merged_gw.csv revision and import it.

    Duplicates ``scripts/backtest_benchwarmers.py``'s own small bootstrap
    rather than importing a private helper across script files -- ``scripts/``
    has no ``__init__.py`` and is not an installed package, so each CLI script
    stays independently runnable.
    """
    client = VaastavClient()
    revisions = client.file_revisions(season, filename="gws/merged_gw.csv")
    if not revisions:
        raise ValueError(f"no merged_gw.csv revisions found for {season}")
    if revision_sha:
        matches = [item for item in revisions if item.sha == revision_sha]
        if not matches:
            raise ValueError("requested revision did not modify merged_gw.csv")
        revision = matches[-1]
    else:
        revision = revisions[-1]

    archive_dir = raw_root / season / revision.sha
    players_path = archive_dir / "players_raw.csv"
    gameweeks_path = archive_dir / "merged_gw.csv"
    if players_path.is_file() and gameweeks_path.is_file():
        players = pd.read_csv(players_path)
        gameweeks = pd.read_csv(gameweeks_path)
    else:
        players = client.csv_at_revision(season, "players_raw.csv", revision)
        gameweeks = client.csv_at_revision(season, "gws/merged_gw.csv", revision)
        archive_dir.mkdir(parents=True, exist_ok=True)
        players.to_csv(players_path, index=False, lineterminator="\n")
        gameweeks.to_csv(gameweeks_path, index=False, lineterminator="\n")

    imported = import_player_fixture_history(
        players_path,
        gameweeks_path,
        season=season,
        source_revision=revision.sha,
        source_committed_at=revision.committed_at,
        database_path=database_path,
    )
    return imported, players, gameweeks


def _bootstrap_block(result) -> dict[str, object]:
    return {
        "point_estimate": result.point_estimate,
        "ci_low": result.ci_low,
        "ci_high": result.ci_high,
        "p_improvement_positive": result.p_improvement_positive,
        "resamples": result.resamples,
        "seed": result.seed,
        "cluster_unit": result.cluster_unit,
        "excludes_zero": result.ci_low > 0.0 or result.ci_high < 0.0,
    }


def run(
    *,
    season: str,
    revision: str | None,
    raw_root: Path,
    evaluation_from_gw: int,
    evaluation_to_gw: int,
    database_path: Path,
    short_form_gameweeks: int,
    defcon_short_form_gameweeks: int,
    long_form_weight: float,
    resamples: int,
    seed: int,
    reference_path: Path,
) -> dict[str, object]:
    reference = load_reference(reference_path)

    imported, players_raw, gameweeks = _archive_and_import(
        season=season,
        revision_sha=revision,
        raw_root=raw_root,
        database_path=database_path,
    )
    gameweeks_with_code = gameweeks.merge(
        players_raw.loc[:, ["id", "code"]],
        left_on="element",
        right_on="id",
        how="left",
    )

    with duckdb.connect(str(database_path)) as connection:
        backtest = materialize_benchwarmers_walk_forward_backtest(
            season=season,
            import_run_id=imported.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_with_code,
            players_raw_frame=players_raw,
            evaluation_from_gw=evaluation_from_gw,
            evaluation_to_gw=evaluation_to_gw,
            short_form_gameweeks=short_form_gameweeks,
            defcon_short_form_gameweeks=defcon_short_form_gameweeks,
            long_form_weight=long_form_weight,
        )

    if not backtest.observations:
        raise ValueError("backtest produced no scored observations to analyze")

    model_metrics = score_predictions(backtest.observations)

    # Raises ValueError (before anything is written) if this run's recomputed
    # aggregate metrics diverge from the reference production backtest.
    self_check = verify_self_check(
        reference=reference,
        reference_path=reference_path,
        import_run_id=imported.import_run_id,
        evaluation_from_gw=evaluation_from_gw,
        evaluation_to_gw=evaluation_to_gw,
        model_metrics=model_metrics,
    )

    naive_observations = matched_naive_observations(backtest.observations, gameweeks_with_code)
    uncertainty = estimate_paired_uncertainty(
        backtest.observations,
        naive_observations,
        resamples=resamples,
        seed=seed,
        include_fixture_sensitivity=True,
    )

    # A second, more specific self-check: the paired point estimates
    # recomputed here must equal the reference's own absolute improvement
    # fields exactly (within machine-noise tolerance), since both express the
    # identical pooled-difference quantity via a different code path.
    reference_mae_improvement = reference.get("absolute_mae_improvement")
    reference_rmse_improvement = reference.get("absolute_rmse_improvement")
    point_estimate_mismatches: list[str] = []
    if reference_mae_improvement is None:
        point_estimate_mismatches.append("absolute_mae_improvement: missing from reference JSON")
    elif abs(uncertainty.mae_point_estimate - reference_mae_improvement) > POINT_ESTIMATE_TOLERANCE:
        point_estimate_mismatches.append(
            f"mae_point_estimate: expected {reference_mae_improvement!r}, "
            f"got {uncertainty.mae_point_estimate!r}"
        )
    if reference_rmse_improvement is None:
        point_estimate_mismatches.append("absolute_rmse_improvement: missing from reference JSON")
    elif (
        abs(uncertainty.rmse_point_estimate - reference_rmse_improvement)
        > POINT_ESTIMATE_TOLERANCE
    ):
        point_estimate_mismatches.append(
            f"rmse_point_estimate: expected {reference_rmse_improvement!r}, "
            f"got {uncertainty.rmse_point_estimate!r}"
        )
    if point_estimate_mismatches:
        joined = "\n  - ".join(point_estimate_mismatches)
        raise ValueError(
            f"paired point-estimate self-check against reference {reference_path} failed -- "
            "this run's recomputed paired MAE/RMSE improvement diverges from the production "
            "backtest's own absolute_mae_improvement/absolute_rmse_improvement, so no "
            "uncertainty output was written:\n  - " + joined
        )

    verdict_state, verdict = interpret_paired_verdict(
        uncertainty.mae_bootstrap, uncertainty.rmse_bootstrap
    )

    return {
        "$schema_note": (
            "Paired, gameweek-cluster bootstrap uncertainty of the 11-component model's "
            "MAE/RMSE advantage over the matched-naive comparator. Read-only measurement; "
            "no scoring/formula/calibration change."
        ),
        "season": season,
        "import_run_id": imported.import_run_id,
        "evaluation_from_gw": evaluation_from_gw,
        "evaluation_to_gw": evaluation_to_gw,
        "self_check": self_check,
        "paired_rows": uncertainty.paired_rows,
        "clusters_gameweek": uncertainty.clusters,
        "mae_point_estimate": uncertainty.mae_point_estimate,
        "rmse_point_estimate": uncertainty.rmse_point_estimate,
        "gameweek_cluster_bootstrap": {
            "mae": _bootstrap_block(uncertainty.mae_bootstrap),
            "rmse": _bootstrap_block(uncertainty.rmse_bootstrap),
        },
        "fixture_cluster_bootstrap": {
            "mae": (
                _bootstrap_block(uncertainty.fixture_cluster_mae_bootstrap)
                if uncertainty.fixture_cluster_mae_bootstrap is not None
                else None
            ),
            "rmse": (
                _bootstrap_block(uncertainty.fixture_cluster_rmse_bootstrap)
                if uncertainty.fixture_cluster_rmse_bootstrap is not None
                else None
            ),
            "note": (
                "Sensitivity check only, clustered by fixture_id instead of gameweek; the "
                "gameweek_cluster_bootstrap block above is the primary result."
            ),
        },
        "verdict": verdict,
        "verdict_state": verdict_state,
        "method_notes": list(build_method_notes(resamples=resamples, seed=seed)),
        "limitations": LIMITATIONS,
        "reproduce": (
            ".venv\\Scripts\\python.exe scripts/analyze_backtest_uncertainty.py --season 2025-26"
        ),
    }


def main() -> None:
    args = parse_args()
    result = run(
        season=args.season,
        revision=args.revision,
        raw_root=args.raw_root,
        evaluation_from_gw=args.evaluation_from_gw,
        evaluation_to_gw=args.evaluation_to_gw,
        database_path=args.database,
        short_form_gameweeks=args.short_form_gameweeks,
        defcon_short_form_gameweeks=args.defcon_short_form_gameweeks,
        long_form_weight=args.long_form_weight,
        resamples=args.resamples,
        seed=args.seed,
        reference_path=args.reference,
    )
    output = json.dumps(result, indent=2, sort_keys=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output + "\n", encoding="utf-8")

    print(f"self_check against {args.reference}: {result['self_check']['status']}")
    print(f"paired_rows={result['paired_rows']} clusters_gameweek={result['clusters_gameweek']}")
    print()
    print("Gameweek-cluster bootstrap (primary):")
    for metric_name in ("mae", "rmse"):
        block = result["gameweek_cluster_bootstrap"][metric_name]
        print(
            f"  {metric_name.upper():4s} point={block['point_estimate']:.4f} "
            f"CI=({block['ci_low']:.4f}, {block['ci_high']:.4f}) "
            f"p(improvement>0)={block['p_improvement_positive']:.3f} "
            f"excludes_zero={block['excludes_zero']}"
        )
    print()
    print(result["verdict"])
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
