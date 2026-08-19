"""Measure walk-forward calibration of the appearance model's own predictions.

The committed causal walk-forward xPts calibration (``scripts/
assess_backtest_calibration.py``) found the model's highest predicted-xPts
band is materially overconfident. `start_probability`/`expected_minutes` and
the per-90 rate components built on top of them are highly correlated in
that band -- a nailed-on starter has high values on both -- so that finding
alone cannot say whether the overprediction traces back to the appearance
model itself or to the rate components.

This is a read-only measurement over ``materialize_benchwarmers_walk_forward_backtest``'s
``appearance_observations`` (the same function ``scripts/backtest_benchwarmers.py``
and ``scripts/assess_backtest_calibration.py`` call) -- it never calls any
``project_benchwarmers_*``/``weight_*`` component function differently, never
introduces a second scoring path, and the calibration slope/intercept it
discovers is REPORTED, not applied to any production projection. Nothing in
``baseline_pipeline.py`` or ``benchwarmers_backtest.py``'s scoring logic is
changed.

Two cohorts, not one:

- ``appearance_eligible`` (primary): every non-DGW player-fixture with a
  causal appearance prediction and a realised starts/minutes label --
  ``backtest.appearance_observations`` as produced by
  ``materialize_benchwarmers_walk_forward_backtest``. This cohort is gated
  ONLY on appearance-model inputs, never on the unrelated xPts requirements
  (player rates, team strength, team IDs, league-average bonus rates) --
  conditioning appearance calibration on xPts scoring success would bias the
  sample toward players/fixtures with complete xPts inputs.
- ``xpts_scored_aligned`` (sensitivity): the subset of ``appearance_eligible``
  whose ``(player_id, fixture_id, gameweek)`` key also produced a
  ``BacktestObservation`` -- i.e. exactly the rows the xPts calibration
  script itself scores. Reported for direct comparability with
  ``assess_backtest_calibration.py``'s own row counts, and to check whether
  restricting to xPts-scored rows changes the appearance-model finding.

For each cohort and each of two targets -- ``start_probability`` (predicted
against realised ``actual_started``) and ``expected_minutes`` (predicted
against realised ``actual_minutes``) -- an OLS calibration slope is fit at
each eligible evaluation gameweek using only strictly earlier gameweeks, then
that SAME per-gameweek fit is applied to that gameweek's own
(unseen-by-the-fit) evaluation rows to produce the causal walk-forward
calibrated prediction reported as ``mae_calibrated``/``mse_calibrated``
(``brier_calibrated`` for ``start_probability``). A separate
``pooled_ols_diagnostic_slope``/``_intercept`` is also reported: one OLS fit
across every eligible gameweek's own out-of-sample evaluation rows pooled
together, purely descriptive of how calibrated the walk-forward predictions
were in aggregate -- this pooled fit is never applied back to its own rows to
produce a prediction, since doing so would be in-sample with respect to the
pooled fit itself (the same mistake already found and fixed in the xPts
calibration script). A slope below 1.0 means predictions are systematically
too extreme (overconfident); it does NOT by itself say whether the mean
prediction is too high or too low -- ``mean_predicted``/``mean_actual``/
``mean_bias`` (predicted minus actual) and their own gameweek-cluster
bootstrap CI are reported separately for that question. Confidence intervals
reuse ``validation.paired_uncertainty.block_bootstrap_statistic``'s
gameweek-block percentile bootstrap rather than a second bespoke resampling
method.

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
from fpl_model.validation.appearance_calibration import (
    DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS,
    AppearanceCalibrationRow,
    appearance_implication_verdict,
    causal_walk_forward_predictions,
    describe_appearance_implication_verdict,
    pooled_walk_forward_slope,
    walk_forward_appearance_calibration,
    xpts_keys_from_backtest_observations,
    xpts_scored_aligned_observations,
)
from fpl_model.validation.backtest import score_predictions
from fpl_model.validation.backtest_self_check import load_reference, verify_self_check
from fpl_model.validation.benchwarmers_backtest import (
    materialize_benchwarmers_walk_forward_backtest,
)
from fpl_model.validation.paired_uncertainty import (
    DEFAULT_RESAMPLES,
    DEFAULT_SEED,
    block_bootstrap_statistic,
)

COHORTS = ("appearance_eligible", "xpts_scored_aligned")
TARGETS = ("start_probability", "expected_minutes")

VALID_RANGE = {
    "start_probability": (0.0, 1.0),
    "expected_minutes": (0.0, 90.0),
}

LIMITATIONS = [
    "Walk-forward calibration with a limited number of eligible gameweeks is still a "
    "small-sample slope estimate; treat point estimates as indicative, not precise.",
    "start_probability calibration is a linear probability model (closed-form OLS on a "
    "0/1 outcome), not logistic regression -- slope < 1 means systematic overconfidence "
    "(predictions too extreme relative to outcomes), but the fitted line is not itself a "
    "valid probability outside [0, 1], and a slope below 1 alone does not say whether the "
    "mean prediction is too high or too low (see mean_bias).",
    "For start_probability specifically, MAE and Brier score (MSE) can disagree in sign: "
    "OLS minimises squared error, so a slope < 1 can reduce Brier score while increasing "
    "MAE (shrinking a confident-and-correct prediction toward the fitted line moves it "
    "further from 0/1). Read brier_calibrated as the primary 'does this calibration "
    "reflect real miscalibration' signal for start_probability; mae_calibrated is "
    "reported for comparability with the xPts calibration script but can be misleading in "
    "isolation for a bounded 0/1 target.",
    "Causal calibrated predictions are NOT clipped to their target's valid range ([0,1] "
    "for start_probability, [0,90] for expected_minutes); out_of_range_calibrated reports "
    "how often and how far a calibrated prediction fell outside that range. Clipping would "
    "be a separate policy decision, not applied here.",
    "No high-probability/high-minutes band split was assessed here (unlike the xPts "
    "calibration's high-predicted-xPts band) -- this script's overall calibration is not a "
    "substitute for a high-band analysis; whether nailed-on starters are calibrated "
    "differently from the rest of the population is an open question this script does not "
    "answer.",
    "A slope below 1.0 measured this way does not by itself justify a production "
    "change without further validation; this script measures and reports only, it does "
    "not apply any calibration to baseline_pipeline.py or benchwarmers_backtest.py.",
    "This is one season (2025-26) only; no cross-season validation exists yet.",
    "This script isolates the appearance model's own calibration; it does not, by "
    "itself, prove the per-90 rate components ARE well calibrated -- only that any "
    "overprediction found here is attributable to start_probability/expected_minutes "
    "specifically, distinct from the rate components built on top of them.",
    "xpts_scored_aligned restricts appearance_eligible to rows that also produced an xPts "
    "observation -- a sensitivity cohort, not the primary result. appearance_eligible is "
    "the primary appearance-model calibration cohort; it is not conditioned on xPts "
    "scoring success (player rates, team strength, team IDs, league-average bonus rates).",
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
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/research/walk_forward_benchwarmers_2025_26_appearance_calibration.json"),
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
    ``scripts/diagnose_backtest_segments.py``, ``scripts/analyze_backtest_uncertainty.py``,
    and ``scripts/assess_backtest_calibration.py`` each already have, rather
    than importing a private helper across script files -- ``scripts/`` has
    no ``__init__.py`` and is not an installed package, so each CLI script
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


def _positions_statistic_factory(position_by_id: dict[int, int]):
    """Return a function that maps a resampled row list to its base-array positions.

    Same pattern as ``scripts/assess_backtest_calibration.py``: resolving
    each resampled row back to its base-array position is an unavoidable
    Python-level pass, but only needs to happen once per resample, not once
    per statistic.
    """

    def _positions(rows: Sequence[AppearanceCalibrationRow]) -> np.ndarray:
        return np.fromiter((position_by_id[id(row)] for row in rows), dtype=np.intp, count=len(rows))

    return _positions


def _slope_statistic_factory(positions_of, predicted_arr: np.ndarray, actual_arr: np.ndarray):
    def _statistic(rows: Sequence[AppearanceCalibrationRow]) -> float:
        positions = positions_of(rows)
        x = predicted_arr[positions]
        y = actual_arr[positions]
        x_bar = np.mean(x)
        y_bar = np.mean(y)
        sxx = np.sum((x - x_bar) ** 2)
        if sxx == 0.0:
            raise ValueError("predicted_value is constant across rows; slope is undefined")
        sxy = np.sum((x - x_bar) * (y - y_bar))
        return float(sxy / sxx)

    return _statistic


def _mean_bias_statistic_factory(positions_of, predicted_arr: np.ndarray, actual_arr: np.ndarray):
    """Return ``mean(predicted - actual)`` as a bootstrap statistic.

    Reported alongside the slope because a slope below 1.0 (systematic
    overconfidence -- predictions too extreme relative to outcomes) is a
    DIFFERENT claim from a positive mean bias (predictions too high on
    average). Neither implies the other in general; both are needed to
    support a claim like 'the model overpredicts starts/minutes on average'.
    """

    def _statistic(rows: Sequence[AppearanceCalibrationRow]) -> float:
        positions = positions_of(rows)
        return float(np.mean(predicted_arr[positions] - actual_arr[positions]))

    return _statistic


def _error_improvement_statistic_factory(
    positions_of,
    baseline_arr: np.ndarray,
    comparison_arr: np.ndarray,
    actual_arr: np.ndarray,
    *,
    power: int,
):
    """Shared MAE (power=1) / MSE (power=2) improvement statistic factory."""

    def _statistic(rows: Sequence[AppearanceCalibrationRow]) -> float:
        positions = positions_of(rows)
        actual = actual_arr[positions]
        baseline_error = np.mean(np.abs(baseline_arr[positions] - actual) ** power)
        comparison_error = np.mean(np.abs(comparison_arr[positions] - actual) ** power)
        return float(baseline_error - comparison_error)

    return _statistic


def _out_of_range_summary(values: np.ndarray, *, low: float, high: float) -> dict[str, object]:
    below = values < low
    above = values > high
    out_of_range = below | above
    return {
        "count": int(np.sum(out_of_range)),
        "min": float(np.min(values)) if values.size else None,
        "max": float(np.max(values)) if values.size else None,
        "min_below_range": float(np.min(values[below])) if np.any(below) else None,
        "max_above_range": float(np.max(values[above])) if np.any(above) else None,
    }


def _target_summary(
    *,
    cohort: str,
    target: str,
    records,
    resamples: int,
    seed: int,
) -> dict[str, object]:
    # Descriptive-only: one OLS fit across every eligible gameweek's own
    # out-of-sample evaluation rows, pooled together. NOT used to produce
    # calibrated predictions (see causal_walk_forward_predictions for that).
    pooled = pooled_walk_forward_slope(records)

    rows, calibrated_predictions = causal_walk_forward_predictions(records)
    if len(rows) < 2:
        raise ValueError(
            f"fewer than 2 causal walk-forward calibrated rows available for "
            f"cohort={cohort!r} target={target!r}"
        )

    position_by_id = {id(r): i for i, r in enumerate(rows)}
    predicted_arr = np.array([r.predicted_value for r in rows], dtype=np.float64)
    actual_arr = np.array([r.actual_value for r in rows], dtype=np.float64)
    calibrated_arr = np.asarray(calibrated_predictions, dtype=np.float64)

    mean_predicted = float(np.mean(predicted_arr))
    mean_actual = float(np.mean(actual_arr))
    mae_raw = float(np.mean(np.abs(predicted_arr - actual_arr)))
    mae_calibrated = float(np.mean(np.abs(calibrated_arr - actual_arr)))
    mse_raw = float(np.mean((predicted_arr - actual_arr) ** 2))
    mse_calibrated = float(np.mean((calibrated_arr - actual_arr) ** 2))

    positions_of = _positions_statistic_factory(position_by_id)

    slope_statistic = _slope_statistic_factory(positions_of, predicted_arr, actual_arr)
    slope_point, slope_ci_low, slope_ci_high, _ = block_bootstrap_statistic(
        rows, statistic=slope_statistic, resamples=resamples, seed=seed
    )

    mean_bias_statistic = _mean_bias_statistic_factory(positions_of, predicted_arr, actual_arr)
    mean_bias_point, mean_bias_ci_low, mean_bias_ci_high, _ = block_bootstrap_statistic(
        rows, statistic=mean_bias_statistic, resamples=resamples, seed=seed
    )

    calibrated_vs_raw_mae_stat = _error_improvement_statistic_factory(
        positions_of, predicted_arr, calibrated_arr, actual_arr, power=1
    )
    cvr_mae_point, cvr_mae_low, cvr_mae_high, _ = block_bootstrap_statistic(
        rows, statistic=calibrated_vs_raw_mae_stat, resamples=resamples, seed=seed
    )

    calibrated_vs_raw_mse_stat = _error_improvement_statistic_factory(
        positions_of, predicted_arr, calibrated_arr, actual_arr, power=2
    )
    cvr_mse_point, cvr_mse_low, cvr_mse_high, _ = block_bootstrap_statistic(
        rows, statistic=calibrated_vs_raw_mse_stat, resamples=resamples, seed=seed
    )

    low, high = VALID_RANGE[target]
    out_of_range_calibrated = _out_of_range_summary(calibrated_arr, low=low, high=high)

    result: dict[str, object] = {
        "cohort": cohort,
        "target": target,
        "pooled_ols_diagnostic_slope": pooled["slope"],
        "pooled_ols_diagnostic_intercept": pooled["intercept"],
        "pooled_ols_diagnostic_note": (
            "Descriptive summary only: one OLS fit across every eligible gameweek's own "
            "out-of-sample evaluation rows, pooled together. Answers 'in aggregate, how "
            "calibrated were the walk-forward predictions' as a single number. This fit is "
            "NOT applied back to its own rows to produce mae_calibrated/mse_calibrated or "
            "either improvement bootstrap below -- doing so would be in-sample with respect "
            "to the pooled fit. mae_calibrated/mse_calibrated instead apply each gameweek's "
            "own prior-gameweeks-only fit (see the trajectory field) to that gameweek's rows. "
            "A slope below 1.0 means predictions are systematically too extreme "
            "(overconfident) relative to outcomes -- it does NOT by itself say whether the "
            "mean prediction is too high or too low; see mean_bias for that separate claim."
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
        "mean_predicted": mean_predicted,
        "mean_actual": mean_actual,
        "mean_bias_note": (
            "mean_bias = mean(predicted - actual): a separate, direct measure of whether "
            "predictions are too high or too low on average, distinct from the slope "
            "(which measures whether predictions are too extreme, not whether they are too "
            "high or too low). A positive mean_bias with a CI entirely above zero is the "
            "correct evidence for an 'overpredicts on average' claim -- slope alone is not."
        ),
        "mean_bias_bootstrap": {
            "point_estimate": mean_bias_point,
            "ci_low": mean_bias_ci_low,
            "ci_high": mean_bias_ci_high,
            "resamples": resamples,
            "seed": seed,
        },
        "mae_raw": mae_raw,
        "mae_calibrated": mae_calibrated,
        "calibrated_vs_raw_mae_improvement": {
            "point_estimate": cvr_mae_point,
            "ci_low": cvr_mae_low,
            "ci_high": cvr_mae_high,
            "resamples": resamples,
            "seed": seed,
        },
        "calibrated_vs_raw_mse_improvement": {
            "point_estimate": cvr_mse_point,
            "ci_low": cvr_mse_low,
            "ci_high": cvr_mse_high,
            "resamples": resamples,
            "seed": seed,
        },
        "out_of_range_calibrated": out_of_range_calibrated,
        "out_of_range_note": (
            f"Causal calibrated predictions are NOT clipped to [{low}, {high}]; this reports "
            "how many fell outside that range and how far, so the reader can judge whether "
            "clipping (a separate policy decision, not applied here) would matter."
        ),
    }
    if target == "start_probability":
        # "Brier score" is the standard name for mean squared error against a
        # binary 0/1 outcome; reported under both names so the JSON is
        # self-explanatory without cross-referencing this docstring.
        result["brier_raw"] = mse_raw
        result["brier_calibrated"] = mse_calibrated
        result["brier_note"] = (
            "Brier score is the standard name for MSE against a binary 0/1 outcome; this is "
            "the primary 'does this calibration reflect real miscalibration' signal for "
            "start_probability (OLS minimises this, not MAE)."
        )
    else:
        result["mse_raw"] = mse_raw
        result["mse_calibrated"] = mse_calibrated
    return result


def _trajectory_for(records) -> list[dict[str, object]]:
    return [
        {
            "gameweek": record.gameweek,
            "slope": record.fit.slope,
            "intercept": record.fit.intercept,
            "training_rows": record.fit.training_rows,
            "training_gameweeks": record.fit.training_gameweeks,
            "evaluation_rows": len(record.evaluation_rows),
        }
        for record in records
    ]


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
    if not backtest.appearance_observations:
        raise ValueError("backtest produced no appearance observations to assess")

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

    xpts_keys = xpts_keys_from_backtest_observations(backtest.observations)
    cohort_observations = {
        "appearance_eligible": backtest.appearance_observations,
        "xpts_scored_aligned": xpts_scored_aligned_observations(
            backtest.appearance_observations, xpts_keys
        ),
    }

    coverage = {
        "candidate_player_fixture_rows": backtest.candidate_player_fixture_rows,
        "dgw_excluded_rows": backtest.dgw_excluded_appearance_rows,
        "missing_appearance_rows": backtest.missing_appearance_rows,
        "appearance_eligible_rows": len(cohort_observations["appearance_eligible"]),
        "xpts_scored_rows": backtest.scored_player_fixture_rows,
        "xpts_scored_aligned_rows": len(cohort_observations["xpts_scored_aligned"]),
    }

    cohort_results: dict[str, dict[str, object]] = {}
    for cohort in COHORTS:
        observations = cohort_observations[cohort]
        target_results: dict[str, dict[str, object]] = {}
        trajectories: dict[str, list[dict[str, object]]] = {}
        for target in TARGETS:
            records = walk_forward_appearance_calibration(
                observations,
                target=target,
                minimum_calibration_gameweeks=minimum_calibration_gameweeks,
            )
            if not records:
                raise ValueError(
                    "no gameweek had enough strictly-prior gameweeks "
                    f"(>= {minimum_calibration_gameweeks}) to fit a walk-forward "
                    f"calibration for cohort={cohort!r} target={target!r}"
                )
            trajectories[target] = _trajectory_for(records)
            target_results[target] = _target_summary(
                cohort=cohort,
                target=target,
                records=records,
                resamples=resamples,
                seed=seed,
            )
        cohort_results[cohort] = {
            "eligible_post_warmup_rows": {
                target: sum(row["evaluation_rows"] for row in trajectories[target])
                for target in TARGETS
            },
            "start_probability": {
                "eligible_gameweeks": [row["gameweek"] for row in trajectories["start_probability"]],
                "trajectory": trajectories["start_probability"],
                **target_results["start_probability"],
            },
            "expected_minutes": {
                "eligible_gameweeks": [row["gameweek"] for row in trajectories["expected_minutes"]],
                "trajectory": trajectories["expected_minutes"],
                **target_results["expected_minutes"],
            },
        }

    primary = cohort_results["appearance_eligible"]

    def _stability_note(ci_low: float, ci_high: float, *, metric: str) -> str:
        if ci_low < 0.0 < ci_high:
            return f"a {metric} CI that crosses zero, so NOT a stable signal at this sample size"
        if ci_high <= 0.0:
            return f"a {metric} CI entirely at or below zero"
        return f"a {metric} CI entirely above zero"

    def _describe(result: dict[str, object]) -> str:
        slope = result["pooled_ols_diagnostic_slope"]
        mean_bias = result["mean_bias_bootstrap"]["point_estimate"]
        bias_ci_low = result["mean_bias_bootstrap"]["ci_low"]
        bias_ci_high = result["mean_bias_bootstrap"]["ci_high"]
        bias_direction = (
            "overpredicts on average"
            if bias_ci_low > 0.0
            else "underpredicts on average"
            if bias_ci_high < 0.0
            else "shows no stable average over/under-prediction at this sample size"
        )
        error_label = "Brier score" if result["target"] == "start_probability" else "MSE"
        error_raw = result.get("brier_raw", result.get("mse_raw"))
        error_calibrated = result.get("brier_calibrated", result.get("mse_calibrated"))
        error_helps = error_calibrated < error_raw
        mse_ci_low = result["calibrated_vs_raw_mse_improvement"]["ci_low"]
        mse_ci_high = result["calibrated_vs_raw_mse_improvement"]["ci_high"]
        return (
            f"{result['cohort']}/{result['target']}: pooled diagnostic slope {slope:.4f} "
            f"(the ideal identity calibration line has slope 1.0 and intercept 0.0 -- slope "
            f"alone means predictions are too extreme, not too high or low). mean_bias "
            f"(predicted-actual) is {mean_bias:.4f}, CI [{bias_ci_low:.4f}, {bias_ci_high:.4f}] "
            f"-- {bias_direction}. By {error_label}, causal calibration "
            f"{'reduces' if error_helps else 'does not reduce'} error "
            f"({error_raw:.4f} raw vs {error_calibrated:.4f} calibrated), "
            f"{_stability_note(mse_ci_low, mse_ci_high, metric=error_label)}"
        )

    def _slope_below_one(result: dict[str, object]) -> bool:
        return result["slope_bootstrap"]["ci_high"] < 1.0

    verdict = appearance_implication_verdict(
        start_probability_ci_below_one=_slope_below_one(primary["start_probability"]),
        expected_minutes_ci_below_one=_slope_below_one(primary["expected_minutes"]),
    )
    interpretation = (
        f"{_describe(primary['start_probability'])}. {_describe(primary['expected_minutes'])}. "
        + describe_appearance_implication_verdict(verdict)
        + " This is a measurement only: no calibration has been applied to any production "
        "projection or scoring formula."
    )

    return {
        "$schema_note": (
            "Walk-forward, out-of-sample calibration of the appearance model's own "
            "predictions (start_probability against realised actual_started; "
            "expected_minutes against realised actual_minutes) -- fit only on "
            "strictly-prior gameweeks at each step, over two cohorts (appearance_eligible: "
            "primary; xpts_scored_aligned: sensitivity). Read-only measurement; no xPts "
            "formula change; not applied to production."
        ),
        "season": season,
        "import_run_id": imported.import_run_id,
        "evaluation_from_gw": evaluation_from_gw,
        "evaluation_to_gw": evaluation_to_gw,
        "self_check": self_check,
        "minimum_calibration_gameweeks": minimum_calibration_gameweeks,
        "coverage": coverage,
        "appearance_eligible": cohort_results["appearance_eligible"],
        "xpts_scored_aligned": cohort_results["xpts_scored_aligned"],
        "interpretation": interpretation,
        "methodology_notes": [
            "appearance_eligible (primary cohort) is every non-DGW player-fixture with a "
            "causal appearance prediction and a realised starts/minutes label -- gated ONLY "
            "on appearance-model inputs, never on the unrelated xPts requirements (player "
            "rates, team strength, team IDs, league-average bonus rates). "
            "xpts_scored_aligned (sensitivity cohort) further restricts to the exact keys "
            "that also produced a BacktestObservation, derived directly from "
            "BacktestObservation's own keys.",
            "Calibration at each gameweek is fit only on strictly-prior gameweeks' "
            "(predicted_value, actual_value) pairs -- never the evaluation gameweek's own "
            "rows or any later gameweek -- mirroring walk_forward_calibration's own causal "
            "training/evaluation split for xPts.",
            "start_probability calibration targets realised actual_started (0.0/1.0); "
            "expected_minutes calibration targets realised actual_minutes -- both read "
            "directly from that gameweek's own merged_gw.csv row, the same source "
            "actual_points is read from.",
            "pooled_ols_diagnostic_slope/intercept pool every eligible gameweek's own "
            "out-of-sample evaluation rows into one final, descriptive-only regression -- "
            "distinct from the trajectory's own fitted-on-prior-data slope values at each "
            "step. This pooled fit is NEVER applied back to its own rows to produce a "
            "prediction. A slope below 1.0 means predictions are too extreme (overconfident), "
            "which is a distinct claim from mean_bias (too high/too low on average) -- both "
            "are reported, neither substitutes for the other.",
            "mae_calibrated/mse_calibrated (brier_calibrated for start_probability) are the "
            "CAUSAL walk-forward calibrated errors: each gameweek's evaluation rows are "
            "calibrated using that gameweek's own fit (fit only on strictly-prior gameweeks, "
            "see the trajectory field), never the pooled_ols_diagnostic fit and never a fit "
            "derived from the row's own eventual outcome.",
            "Confidence intervals reuse the gameweek block bootstrap from "
            "validation.paired_uncertainty (block_bootstrap_statistic), applied to the "
            "causal calibrated-prediction rows grouped by the gameweek at which they were "
            "scored.",
            "No high-probability/high-minutes band split was assessed (unlike the xPts "
            "calibration's high-predicted-xPts band); this script's overall calibration "
            "does not itself say anything about how nailed-on starters are calibrated.",
            "Causal calibrated predictions are not clipped to their target's valid range; "
            "out_of_range_calibrated reports how often and how far a calibrated prediction "
            "fell outside [0,1] (start_probability) or [0,90] (expected_minutes).",
        ],
        "limitations": LIMITATIONS,
        "reproduce": (
            ".venv\\Scripts\\python.exe scripts/assess_appearance_calibration.py --season 2025-26"
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
        resamples=args.resamples,
        seed=args.seed,
        reference_path=args.reference,
    )
    output = json.dumps(result, indent=2, sort_keys=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output + "\n", encoding="utf-8")

    print(f"self_check against {args.reference}: {result['self_check']['status']}")
    print(f"coverage: {result['coverage']}")
    print()
    for cohort in COHORTS:
        for target in TARGETS:
            section = result[cohort][target]
            error_label = "brier" if target == "start_probability" else "mse"
            error_raw = section.get("brier_raw", section.get("mse_raw"))
            error_calibrated = section.get("brier_calibrated", section.get("mse_calibrated"))
            print(
                f"{cohort}/{target}: rows={section['evaluation_rows']} "
                f"diagnostic_slope={section['pooled_ols_diagnostic_slope']:.4f} "
                f"diagnostic_intercept={section['pooled_ols_diagnostic_intercept']:.4f}"
            )
            print(
                f"  mean_predicted={section['mean_predicted']:.4f} "
                f"mean_actual={section['mean_actual']:.4f} "
                f"mean_bias={section['mean_bias_bootstrap']['point_estimate']:.4f} "
                f"ci=[{section['mean_bias_bootstrap']['ci_low']:.4f}, "
                f"{section['mean_bias_bootstrap']['ci_high']:.4f}]"
            )
            print(
                f"  mae_raw={section['mae_raw']:.4f} mae_calibrated={section['mae_calibrated']:.4f} "
                f"{error_label}_raw={error_raw:.4f} {error_label}_calibrated={error_calibrated:.4f}"
            )
            print(
                "  calibrated_vs_raw_mse_improvement: "
                f"point={section['calibrated_vs_raw_mse_improvement']['point_estimate']:.4f} "
                f"ci=[{section['calibrated_vs_raw_mse_improvement']['ci_low']:.4f}, "
                f"{section['calibrated_vs_raw_mse_improvement']['ci_high']:.4f}]"
            )
            print(f"  out_of_range_calibrated: {section['out_of_range_calibrated']}")
    print()
    print(result["interpretation"])
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
