"""Measure walk-forward xPts calibration -- fit only on strictly-prior gameweeks.

This is a read-only measurement over already-scored observations from
``materialize_benchwarmers_walk_forward_backtest`` (the same function
``scripts/backtest_benchwarmers.py`` calls) -- it never calls any
``project_benchwarmers_*``/``weight_*`` component function differently, never
introduces a second scoring path, and the calibration slope/intercept it
discovers is REPORTED, not applied to any production projection. Nothing in
``baseline_pipeline.py`` or ``benchwarmers_backtest.py`` is changed.

For each evaluation gameweek, a calibration slope (``actual_points ~
predicted_xpts``) is fit using only strictly earlier gameweeks -- the same
no-lookahead boundary every other backtest input in this codebase already
enforces -- then that SAME per-gameweek fit is applied to that gameweek's own
(unseen-by-the-fit) evaluation rows to produce the causal walk-forward
calibrated prediction reported as ``mae_calibrated``. A separate
``pooled_ols_diagnostic_slope``/``_intercept`` is also reported: one OLS fit
across every eligible gameweek's own out-of-sample evaluation rows pooled
together, purely descriptive of how calibrated the walk-forward predictions
were in aggregate -- this pooled fit is never applied back to its own rows to
produce a prediction, since doing so would be in-sample with respect to the
pooled fit itself. Both are computed overall and restricted to the highest
predicted-xPts band (also determined only from prior data at each step).
Confidence intervals reuse
``validation.paired_uncertainty.block_bootstrap_statistic``'s gameweek-block
percentile bootstrap rather than a second bespoke resampling method.

Before any calibration work begins, this run's recomputed aggregate backtest
metrics are compared against ``--reference`` (defaulting to the committed
``docs/research/walk_forward_benchwarmers_2025_26.json``) via
``validation.backtest_self_check.verify_self_check``. A mismatch raises
``ValueError`` and nothing is written.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

import duckdb
import numpy as np
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
    block_bootstrap_statistic,
)
from fpl_model.validation.walk_forward_calibration import (
    DEFAULT_HIGH_BAND_PERCENTILE,
    DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS,
    CalibrationRow,
    causal_walk_forward_predictions,
    pooled_walk_forward_slope,
    walk_forward_calibration,
)

LIMITATIONS = [
    "Walk-forward calibration with a limited number of eligible gameweeks is still a "
    "small-sample slope estimate; treat point estimates as indicative, not precise.",
    "The high-prediction-band threshold is recomputed from each step's own prior-fold "
    "pool, so early eligible gameweeks may have very few high-band training rows -- "
    "some steps' high_band_fit is None rather than fabricated from too little data.",
    "A slope below 1.0 measured this way does not by itself justify a production "
    "change without further validation; this script measures and reports only, it does "
    "not apply any calibration to baseline_pipeline.py or benchwarmers_backtest.py.",
    "This is one season (2025-26) only; no cross-season validation exists yet.",
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
    parser.add_argument(
        "--minimum-calibration-gameweeks",
        type=int,
        default=DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS,
    )
    parser.add_argument(
        "--high-band-percentile", type=float, default=DEFAULT_HIGH_BAND_PERCENTILE
    )
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/research/walk_forward_benchwarmers_2025_26_calibration.json"),
    )
    parser.add_argument(
        "--reference",
        type=Path,
        default=Path("docs/research/walk_forward_benchwarmers_2025_26.json"),
        help=(
            "Production backtest JSON this run's aggregate metrics must match "
            "before any calibration work begins."
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

    Duplicates the same small bootstrap ``scripts/backtest_benchwarmers.py``,
    ``scripts/diagnose_backtest_segments.py``, and
    ``scripts/analyze_backtest_uncertainty.py`` each already have, rather than
    importing a private helper across script files -- ``scripts/`` has no
    ``__init__.py`` and is not an installed package, so each CLI script stays
    independently runnable.
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


def _naive_prediction_by_key(
    naive_observations, keys: set[tuple[object, object, int]]
) -> dict[tuple[object, object, int], float]:
    result = {}
    for observation in naive_observations:
        key = (observation.player_id, observation.fixture_id, observation.gameweek)
        if key in keys:
            result[key] = observation.predicted_xpts
    return result


def _positions_statistic_factory(position_by_id: dict[int, int]):
    """Return a function that maps a resampled row list to its base-array positions.

    ``block_bootstrap_statistic`` hands every ``statistic`` callable a plain
    Python list of ``CalibrationRow`` objects (with repeats) on each of
    ``resamples`` draws -- resolving each row back to "which of the original
    ~14,500 (overall band) or ~3,000 (high band) pooled rows is this" is an
    unavoidable Python-level pass, but it only needs to happen once per
    resample, not once per statistic. Every
    other array (predicted/actual/calibrated/naive values, all aligned to
    the same base row order as ``position_by_id``) can then be gathered via
    pure numpy fancy-indexing, so a single ``positions`` array is shared by
    the slope statistic and both MAE-improvement statistics below.
    """

    def _positions(rows: Sequence[CalibrationRow]) -> np.ndarray:
        return np.fromiter((position_by_id[id(row)] for row in rows), dtype=np.intp, count=len(rows))

    return _positions


def _slope_statistic_factory(positions_of, predicted_arr: np.ndarray, actual_arr: np.ndarray):
    def _statistic(rows: Sequence[CalibrationRow]) -> float:
        positions = positions_of(rows)
        x = predicted_arr[positions]
        y = actual_arr[positions]
        x_bar = np.mean(x)
        y_bar = np.mean(y)
        sxx = np.sum((x - x_bar) ** 2)
        if sxx == 0.0:
            raise ValueError("predicted_xpts is constant across rows; slope is undefined")
        sxy = np.sum((x - x_bar) * (y - y_bar))
        return float(sxy / sxx)

    return _statistic


def _mae_improvement_statistic_factory(
    positions_of, baseline_arr: np.ndarray, comparison_arr: np.ndarray, actual_arr: np.ndarray
):
    """Return a numpy-vectorised MAE-improvement statistic for ``block_bootstrap_statistic``.

    Reuses the same ``positions_of`` row->base-array-position resolver the
    slope statistic uses (see ``_positions_statistic_factory``), then does
    all arithmetic as numpy fancy-indexing rather than a per-row Python loop
    -- a per-resample ``for row in rows`` loop over ~14,500 rows x 10,000
    resamples (~145M Python-level iterations for the overall band alone)
    dominated this script's runtime before this optimisation, regardless of
    how each row's value was looked up (dict-keyed by id() alone was not
    materially faster than a composite tuple key; the per-row Python
    overhead itself was the cost).
    """

    def _statistic(rows: Sequence[CalibrationRow]) -> float:
        positions = positions_of(rows)
        actual = actual_arr[positions]
        baseline_mae = np.mean(np.abs(baseline_arr[positions] - actual))
        comparison_mae = np.mean(np.abs(comparison_arr[positions] - actual))
        return float(baseline_mae - comparison_mae)

    return _statistic


def _band_summary(
    *,
    band: str,
    records,
    naive_by_key: dict,
    resamples: int,
    seed: int,
) -> dict[str, object]:
    # Descriptive-only: one OLS fit across every eligible gameweek's own
    # out-of-sample evaluation rows, pooled together. This answers "in
    # aggregate, how calibrated were the walk-forward predictions" as a
    # single summary number -- it is NOT used to produce calibrated
    # predictions (see _causal_walk_forward_predictions for that), because
    # applying a fit derived from a row's own eventual outcome back onto
    # that same row would be in-sample with respect to the calibrator.
    pooled = pooled_walk_forward_slope(records, band=band)

    rows, calibrated_predictions = causal_walk_forward_predictions(records, band=band)
    if len(rows) < 2:
        raise ValueError(
            f"fewer than 2 causal walk-forward calibrated rows available for band={band!r}"
        )

    # A single position_by_id map (row identity -> index into these base arrays)
    # is shared by every statistic below via _positions_statistic_factory, so the
    # Python-level id()-resolution pass happens once per resample instead of once
    # per statistic per resample; everything past that is pure numpy fancy-indexing.
    position_by_id = {id(r): i for i, r in enumerate(rows)}
    predicted_arr = np.array([r.predicted_xpts for r in rows], dtype=np.float64)
    actual_arr = np.array([r.actual_points for r in rows], dtype=np.float64)
    calibrated_arr = np.asarray(calibrated_predictions, dtype=np.float64)
    naive_arr = np.array(
        [naive_by_key[(r.player_id, r.fixture_id, r.gameweek)] for r in rows], dtype=np.float64
    )

    mae_raw = float(np.mean(np.abs(predicted_arr - actual_arr)))
    mae_calibrated = float(np.mean(np.abs(calibrated_arr - actual_arr)))
    mae_naive = float(np.mean(np.abs(naive_arr - actual_arr)))

    positions_of = _positions_statistic_factory(position_by_id)

    slope_statistic = _slope_statistic_factory(positions_of, predicted_arr, actual_arr)
    slope_point, slope_ci_low, slope_ci_high, _ = block_bootstrap_statistic(
        rows, statistic=slope_statistic, resamples=resamples, seed=seed
    )

    calibrated_vs_raw_stat = _mae_improvement_statistic_factory(
        positions_of, predicted_arr, calibrated_arr, actual_arr
    )
    cvr_point, cvr_low, cvr_high, _ = block_bootstrap_statistic(
        rows, statistic=calibrated_vs_raw_stat, resamples=resamples, seed=seed
    )

    calibrated_vs_naive_stat = _mae_improvement_statistic_factory(
        positions_of, naive_arr, calibrated_arr, actual_arr
    )
    cvn_point, cvn_low, cvn_high, _ = block_bootstrap_statistic(
        rows, statistic=calibrated_vs_naive_stat, resamples=resamples, seed=seed
    )

    return {
        # Descriptive-only diagnostic: one OLS fit across every eligible
        # gameweek's own out-of-sample evaluation rows, pooled together. NOT
        # used to produce mae_calibrated or either improvement bootstrap
        # below -- see pooled_ols_diagnostic_note and
        # _causal_walk_forward_predictions.
        "pooled_ols_diagnostic_slope": pooled["slope"],
        "pooled_ols_diagnostic_intercept": pooled["intercept"],
        "pooled_ols_diagnostic_note": (
            "Descriptive summary only: one OLS fit across every eligible gameweek's own "
            "out-of-sample evaluation rows, pooled together. Answers 'in aggregate, how "
            "calibrated were the walk-forward predictions' as a single number. This fit is "
            "NOT applied back to its own rows to produce mae_calibrated or the improvement "
            "bootstraps below -- doing so would be in-sample with respect to the pooled fit. "
            "mae_calibrated instead applies each gameweek's own prior-gameweeks-only "
            "overall_fit/high_band_fit (see the trajectory field) to that gameweek's rows."
        ),
        "evaluation_rows": pooled["evaluation_rows"],
        "eligible_gameweeks": pooled["eligible_gameweeks"],
        "slope_bootstrap": {
            "point_estimate": slope_point,
            "ci_low": slope_ci_low,
            "ci_high": slope_ci_high,
            "resamples": resamples,
            "seed": seed,
        },
        "mae_raw": mae_raw,
        "mae_calibrated": mae_calibrated,
        "mae_calibrated_note": (
            "Causal walk-forward calibrated MAE: each gameweek's evaluation rows are "
            "calibrated using that gameweek's own overall_fit/high_band_fit, trained only on "
            "strictly-prior gameweeks -- never the pooled diagnostic fit above."
        ),
        "mae_naive": mae_naive,
        "calibrated_vs_raw_improvement": {
            "point_estimate": cvr_point,
            "ci_low": cvr_low,
            "ci_high": cvr_high,
            "resamples": resamples,
            "seed": seed,
        },
        "calibrated_vs_naive_improvement": {
            "point_estimate": cvn_point,
            "ci_low": cvn_low,
            "ci_high": cvn_high,
            "resamples": resamples,
            "seed": seed,
        },
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
    minimum_calibration_gameweeks: int,
    high_band_percentile: float,
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
        raise ValueError("backtest produced no scored observations to assess")

    model_metrics = score_predictions(backtest.observations)

    # Raises ValueError (before any calibration work) if this run's
    # recomputed aggregate metrics diverge from the reference backtest.
    self_check = verify_self_check(
        reference=reference,
        reference_path=reference_path,
        import_run_id=imported.import_run_id,
        evaluation_from_gw=evaluation_from_gw,
        evaluation_to_gw=evaluation_to_gw,
        model_metrics=model_metrics,
    )

    records = walk_forward_calibration(
        backtest.observations,
        minimum_calibration_gameweeks=minimum_calibration_gameweeks,
        high_band_percentile=high_band_percentile,
    )
    if not records:
        raise ValueError(
            "no gameweek had enough strictly-prior gameweeks "
            f"(>= {minimum_calibration_gameweeks}) to fit a walk-forward calibration"
        )

    naive_observations = matched_naive_observations(backtest.observations, gameweeks_with_code)
    all_keys = {
        (row.player_id, row.fixture_id, row.gameweek)
        for record in records
        for row in record.overall_evaluation_rows
    }
    naive_by_key = _naive_prediction_by_key(naive_observations, all_keys)

    pooled_overall = _band_summary(
        band="overall",
        records=records,
        naive_by_key=naive_by_key,
        resamples=resamples,
        seed=seed,
    )
    high_band_eligible = [r for r in records if r.high_band_fit is not None]
    pooled_high_band = (
        _band_summary(
            band="high",
            records=records,
            naive_by_key=naive_by_key,
            resamples=resamples,
            seed=seed,
        )
        if high_band_eligible
        else None
    )

    trajectory = [
        {
            "gameweek": record.gameweek,
            "overall_slope": record.overall_fit.slope,
            "overall_intercept": record.overall_fit.intercept,
            "overall_training_rows": record.overall_fit.training_rows,
            "overall_training_gameweeks": record.overall_fit.training_gameweeks,
            "overall_evaluation_rows": len(record.overall_evaluation_rows),
            "high_band_threshold": record.high_band_threshold,
            "high_band_slope": record.high_band_fit.slope if record.high_band_fit else None,
            "high_band_intercept": (
                record.high_band_fit.intercept if record.high_band_fit else None
            ),
            "high_band_available": record.high_band_fit is not None,
            "high_band_evaluation_rows": len(record.high_band_evaluation_rows),
        }
        for record in records
    ]

    high_band_note = (
        f"pooled_high_band.pooled_ols_diagnostic_slope = "
        f"{pooled_high_band['pooled_ols_diagnostic_slope']:.4f}, materially below "
        f"pooled_overall.pooled_ols_diagnostic_slope = "
        f"{pooled_overall['pooled_ols_diagnostic_slope']:.4f}"
        if pooled_high_band is not None
        and pooled_high_band["pooled_ols_diagnostic_slope"]
        < pooled_overall["pooled_ols_diagnostic_slope"]
        else "the high-prediction band's diagnostic slope is not materially below the "
        "overall diagnostic slope in this sample"
    )
    calibration_helps_overall = pooled_overall["mae_calibrated"] < pooled_overall["mae_raw"]
    overall_ci_low = pooled_overall["calibrated_vs_raw_improvement"]["ci_low"]
    overall_ci_high = pooled_overall["calibrated_vs_raw_improvement"]["ci_high"]
    overall_ci_crosses_zero = overall_ci_low < 0.0 < overall_ci_high
    overall_stability_note = (
        "this overall improvement's bootstrap CI "
        f"[{overall_ci_low:.4f}, {overall_ci_high:.4f}] crosses zero, so it is NOT a "
        "stable signal at this sample size -- treat the overall point estimate as "
        "indicative only"
        if overall_ci_crosses_zero
        else "this overall improvement's bootstrap CI "
        f"[{overall_ci_low:.4f}, {overall_ci_high:.4f}] lies entirely above zero"
    )
    interpretation = (
        f"Pooled walk-forward overall diagnostic slope is "
        f"{pooled_overall['pooled_ols_diagnostic_slope']:.4f} (1.0 = perfectly calibrated); "
        f"{high_band_note}. Causal walk-forward calibration (each gameweek's own "
        f"prior-gameweeks-only fit applied to that gameweek's evaluation rows) "
        f"{'reduces' if calibration_helps_overall else 'does not reduce'} pooled MAE overall "
        f"({pooled_overall['mae_raw']:.4f} raw vs {pooled_overall['mae_calibrated']:.4f} "
        f"calibrated); {overall_stability_note}. "
        "This is a measurement only: no calibration has been applied to any production "
        "projection or scoring formula."
    )

    return {
        "$schema_note": (
            "Walk-forward, out-of-sample calibration of predicted_xpts against "
            "actual_points -- fit only on strictly-prior gameweeks at each step. "
            "Read-only measurement; no xPts formula change; not applied to production."
        ),
        "season": season,
        "import_run_id": imported.import_run_id,
        "evaluation_from_gw": evaluation_from_gw,
        "evaluation_to_gw": evaluation_to_gw,
        "self_check": self_check,
        "minimum_calibration_gameweeks": minimum_calibration_gameweeks,
        "high_band_percentile": high_band_percentile,
        "eligible_gameweeks": [record.gameweek for record in records],
        "trajectory": trajectory,
        "pooled_overall": pooled_overall,
        "pooled_high_band": pooled_high_band,
        "interpretation": interpretation,
        "methodology_notes": [
            "Calibration at each gameweek is fit only on strictly-prior gameweeks' "
            "(predicted_xpts, actual_points) pairs -- never the evaluation gameweek's own "
            "rows or any later gameweek -- mirroring walk_forward_folds' own causal "
            "training/evaluation split.",
            "The high-prediction-band threshold is recomputed from each step's own "
            "prior-fold pool (numpy.percentile), not a full-season quartile boundary, to "
            "avoid leaking future gameweeks' predicted_xpts distribution into an early "
            "gameweek's band definition.",
            "pooled_ols_diagnostic_slope/intercept pool every eligible gameweek's own "
            "out-of-sample evaluation rows into one final, descriptive-only regression -- a "
            "summary of what was actually achieved walk-forward, distinct from the "
            "trajectory's own fitted-on-prior-data slope values at each step. This pooled fit "
            "is NEVER applied back to its own rows to produce a prediction.",
            "mae_calibrated is the CAUSAL walk-forward calibrated MAE: each gameweek's "
            "evaluation rows are calibrated using that gameweek's own overall_fit/"
            "high_band_fit (fit only on strictly-prior gameweeks, see the trajectory field), "
            "never the pooled_ols_diagnostic fit and never a fit derived from the row's own "
            "eventual outcome. This differs from mae_raw/mae_naive, which need no fit at all.",
            "Confidence intervals reuse the gameweek block bootstrap from "
            "validation.paired_uncertainty (block_bootstrap_statistic), applied to the "
            "causal calibrated-prediction rows grouped by the gameweek at which they were "
            "scored.",
        ],
        "limitations": LIMITATIONS,
        "reproduce": (
            ".venv\\Scripts\\python.exe scripts/assess_backtest_calibration.py --season 2025-26"
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
        minimum_calibration_gameweeks=args.minimum_calibration_gameweeks,
        high_band_percentile=args.high_band_percentile,
        resamples=args.resamples,
        seed=args.seed,
        reference_path=args.reference,
    )
    output = json.dumps(result, indent=2, sort_keys=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output + "\n", encoding="utf-8")

    print(f"self_check against {args.reference}: {result['self_check']['status']}")
    print(f"eligible_gameweeks: {result['eligible_gameweeks']}")
    print()
    print(
        f"pooled_overall: rows={result['pooled_overall']['evaluation_rows']} "
        f"diagnostic_slope={result['pooled_overall']['pooled_ols_diagnostic_slope']:.4f} "
        f"diagnostic_intercept={result['pooled_overall']['pooled_ols_diagnostic_intercept']:.4f} "
        f"mae_raw={result['pooled_overall']['mae_raw']:.4f} "
        f"mae_calibrated={result['pooled_overall']['mae_calibrated']:.4f} "
        f"mae_naive={result['pooled_overall']['mae_naive']:.4f}"
    )
    print(
        "  calibrated_vs_raw_improvement: "
        f"point={result['pooled_overall']['calibrated_vs_raw_improvement']['point_estimate']:.4f} "
        f"ci=[{result['pooled_overall']['calibrated_vs_raw_improvement']['ci_low']:.4f}, "
        f"{result['pooled_overall']['calibrated_vs_raw_improvement']['ci_high']:.4f}]"
    )
    if result["pooled_high_band"] is not None:
        print(
            f"pooled_high_band: rows={result['pooled_high_band']['evaluation_rows']} "
            f"diagnostic_slope={result['pooled_high_band']['pooled_ols_diagnostic_slope']:.4f} "
            "diagnostic_intercept="
            f"{result['pooled_high_band']['pooled_ols_diagnostic_intercept']:.4f} "
            f"mae_raw={result['pooled_high_band']['mae_raw']:.4f} "
            f"mae_calibrated={result['pooled_high_band']['mae_calibrated']:.4f} "
            f"mae_naive={result['pooled_high_band']['mae_naive']:.4f}"
        )
    print()
    print(result["interpretation"])
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
