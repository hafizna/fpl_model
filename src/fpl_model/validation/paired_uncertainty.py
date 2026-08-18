"""Paired, cluster-aware bootstrap uncertainty for the model-vs-naive comparator.

Quantifies whether the 11-component model's MAE/RMSE advantage over
``matched_naive_observations`` is a stable signal or noise from one season's
36 gameweeks -- pure measurement, no scoring/formula/calibration change.

Rows are not independent: multiple players in the same gameweek share
causally-derived team-strength/rate/appearance inputs, and the same player
recurs across gameweeks. An i.i.d. per-row bootstrap would understate
uncertainty by ignoring this. The primary method therefore resamples whole
gameweeks (the same causal-fold unit ``walk_forward_folds`` already uses),
not individual rows; a fixture-clustered bootstrap is available as a
labelled sensitivity check, never as the primary result.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import sqrt
from typing import Literal

import numpy as np

from fpl_model.validation.backtest import BacktestObservation

DEFAULT_RESAMPLES = 10_000
DEFAULT_SEED = 42

VerdictState = Literal["advantage", "disadvantage", "mixed"]


@dataclass(frozen=True, slots=True)
class PairedRow:
    """One player-fixture's model and matched-naive prediction error.

    ``error = predicted_xpts - actual_points`` for each side, sign-preserving
    (positive = overprediction). Kept signed rather than pre-absoluted so the
    record stays reusable for statistics other than MAE.
    """

    player_id: int | str
    fixture_id: int | str
    gameweek: int
    model_error: float
    naive_error: float


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """One metric's point estimate, percentile CI, and resample diagnostics."""

    point_estimate: float
    ci_low: float
    ci_high: float
    p_improvement_positive: float
    resamples: int
    seed: int
    cluster_unit: str


@dataclass(frozen=True, slots=True)
class PairedUncertaintyResult:
    mae_point_estimate: float
    rmse_point_estimate: float
    mae_bootstrap: BootstrapResult
    rmse_bootstrap: BootstrapResult
    fixture_cluster_mae_bootstrap: BootstrapResult | None
    fixture_cluster_rmse_bootstrap: BootstrapResult | None
    paired_rows: int
    clusters: int


def build_paired_rows(
    model_observations: Sequence[BacktestObservation],
    naive_observations: Sequence[BacktestObservation],
) -> tuple[PairedRow, ...]:
    """Join model and matched-naive observations by ``(player_id, fixture_id, gameweek)``.

    Raises ``ValueError`` if either side has a duplicate key, if the key sets
    differ (missing/extra pairs), or if a matched pair disagrees on
    ``actual_points`` -- the same underlying row must have the same realised
    outcome on both sides; a disagreement is a real data-integrity problem,
    not a formality.
    """

    def _index(
        observations: Sequence[BacktestObservation], label: str
    ) -> dict[tuple[object, object, int], BacktestObservation]:
        index: dict[tuple[object, object, int], BacktestObservation] = {}
        duplicates: list[tuple[object, object, int]] = []
        for observation in observations:
            key = (observation.player_id, observation.fixture_id, observation.gameweek)
            if key in index:
                duplicates.append(key)
            index[key] = observation
        if duplicates:
            raise ValueError(
                f"{label} observations contain {len(duplicates)} duplicate key(s), "
                f"e.g. {duplicates[0]!r}"
            )
        return index

    model_index = _index(model_observations, "model")
    naive_index = _index(naive_observations, "naive")

    model_keys = set(model_index)
    naive_keys = set(naive_index)
    if model_keys != naive_keys:
        missing_from_naive = model_keys - naive_keys
        missing_from_model = naive_keys - model_keys
        raise ValueError(
            "model and naive observation key sets differ: "
            f"{len(missing_from_naive)} key(s) missing from naive, "
            f"{len(missing_from_model)} key(s) missing from model "
            f"(e.g. missing_from_naive={next(iter(missing_from_naive), None)!r}, "
            f"missing_from_model={next(iter(missing_from_model), None)!r})"
        )

    rows: list[PairedRow] = []
    mismatched_actuals: list[tuple[object, object, int]] = []
    for key in model_keys:
        model_observation = model_index[key]
        naive_observation = naive_index[key]
        if model_observation.actual_points != naive_observation.actual_points:
            mismatched_actuals.append(key)
            continue
        player_id, fixture_id, gameweek = key
        rows.append(
            PairedRow(
                player_id=player_id,
                fixture_id=fixture_id,
                gameweek=gameweek,
                model_error=model_observation.predicted_xpts - model_observation.actual_points,
                naive_error=naive_observation.predicted_xpts - naive_observation.actual_points,
            )
        )
    if mismatched_actuals:
        raise ValueError(
            f"{len(mismatched_actuals)} matched key(s) disagree on actual_points between "
            f"model and naive observations, e.g. {mismatched_actuals[0]!r}"
        )

    rows.sort(key=lambda row: (row.gameweek, str(row.fixture_id), str(row.player_id)))
    return tuple(rows)


def _pooled_mae(rows: Sequence[PairedRow]) -> tuple[float, float]:
    """Return ``(model_mae, naive_mae)`` pooled over ``rows`` (score_predictions' formula)."""
    if not rows:
        raise ValueError("at least one paired row is required")
    model_mae = sum(abs(row.model_error) for row in rows) / len(rows)
    naive_mae = sum(abs(row.naive_error) for row in rows) / len(rows)
    return model_mae, naive_mae


def _pooled_rmse(rows: Sequence[PairedRow]) -> tuple[float, float]:
    """Return ``(model_rmse, naive_rmse)`` pooled over ``rows`` (score_predictions' formula)."""
    if not rows:
        raise ValueError("at least one paired row is required")
    model_rmse = sqrt(sum(row.model_error**2 for row in rows) / len(rows))
    naive_rmse = sqrt(sum(row.naive_error**2 for row in rows) / len(rows))
    return model_rmse, naive_rmse


def cluster_bootstrap(
    paired_rows: Sequence[PairedRow],
    *,
    cluster_key: Callable[[PairedRow], object],
    cluster_label: str,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
) -> tuple[BootstrapResult, BootstrapResult]:
    """Percentile block-bootstrap CI for MAE and RMSE improvement, clustered by ``cluster_key``.

    Positive improvement means the model beats the naive comparator
    (``naive_metric - model_metric``). Each of ``resamples`` iterations draws
    cluster labels with replacement and pools every row belonging to each
    drawn cluster (a cluster drawn twice contributes its rows twice) before
    recomputing MAE/RMSE on the pooled resample -- RMSE is not additive
    across rows/clusters, so it must be recomputed on the pooled set each
    time, never averaged per-cluster. The point estimate is computed once on
    the true (unresampled) full row set.
    """
    if not paired_rows:
        raise ValueError("at least one paired row is required")
    if resamples < 1:
        raise ValueError("resamples must be a positive integer")

    clusters: dict[object, list[PairedRow]] = {}
    for row in paired_rows:
        clusters.setdefault(cluster_key(row), []).append(row)
    cluster_labels = sorted(clusters, key=str)
    cluster_count = len(cluster_labels)

    model_mae, naive_mae = _pooled_mae(paired_rows)
    model_rmse, naive_rmse = _pooled_rmse(paired_rows)
    mae_point_estimate = naive_mae - model_mae
    rmse_point_estimate = naive_rmse - model_rmse

    # Vectorised resampling: pack each cluster's errors into flat numpy
    # arrays once, then build every resample's pooled index array via
    # numpy concatenation instead of Python-object list building/iteration
    # (roughly 25x faster at this dataset's scale -- ~16.5k rows x 10k
    # resamples -- verified during development).
    cluster_model_errors = [
        np.fromiter((row.model_error for row in clusters[label]), dtype=np.float64)
        for label in cluster_labels
    ]
    cluster_naive_errors = [
        np.fromiter((row.naive_error for row in clusters[label]), dtype=np.float64)
        for label in cluster_labels
    ]

    rng = np.random.default_rng(seed)
    mae_improvements = np.empty(resamples, dtype=np.float64)
    rmse_improvements = np.empty(resamples, dtype=np.float64)
    label_indices = rng.integers(0, cluster_count, size=(resamples, cluster_count))
    for resample_index in range(resamples):
        drawn = label_indices[resample_index]
        pooled_model = np.concatenate([cluster_model_errors[i] for i in drawn])
        pooled_naive = np.concatenate([cluster_naive_errors[i] for i in drawn])
        resample_model_mae = np.mean(np.abs(pooled_model))
        resample_naive_mae = np.mean(np.abs(pooled_naive))
        resample_model_rmse = np.sqrt(np.mean(pooled_model**2))
        resample_naive_rmse = np.sqrt(np.mean(pooled_naive**2))
        mae_improvements[resample_index] = resample_naive_mae - resample_model_mae
        rmse_improvements[resample_index] = resample_naive_rmse - resample_model_rmse

    mae_ci_low, mae_ci_high = np.percentile(mae_improvements, [2.5, 97.5])
    rmse_ci_low, rmse_ci_high = np.percentile(rmse_improvements, [2.5, 97.5])

    mae_result = BootstrapResult(
        point_estimate=float(mae_point_estimate),
        ci_low=float(mae_ci_low),
        ci_high=float(mae_ci_high),
        p_improvement_positive=float(np.mean(mae_improvements > 0.0)),
        resamples=resamples,
        seed=seed,
        cluster_unit=cluster_label,
    )
    rmse_result = BootstrapResult(
        point_estimate=float(rmse_point_estimate),
        ci_low=float(rmse_ci_low),
        ci_high=float(rmse_ci_high),
        p_improvement_positive=float(np.mean(rmse_improvements > 0.0)),
        resamples=resamples,
        seed=seed,
        cluster_unit=cluster_label,
    )
    return mae_result, rmse_result


def estimate_paired_uncertainty(
    model_observations: Sequence[BacktestObservation],
    naive_observations: Sequence[BacktestObservation],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = DEFAULT_SEED,
    include_fixture_sensitivity: bool = True,
) -> PairedUncertaintyResult:
    """Build paired rows and run the primary (gameweek) and optional sensitivity (fixture) bootstraps."""
    paired_rows = build_paired_rows(model_observations, naive_observations)

    mae_bootstrap, rmse_bootstrap = cluster_bootstrap(
        paired_rows,
        cluster_key=lambda row: row.gameweek,
        cluster_label="gameweek",
        resamples=resamples,
        seed=seed,
    )

    fixture_mae_bootstrap: BootstrapResult | None = None
    fixture_rmse_bootstrap: BootstrapResult | None = None
    if include_fixture_sensitivity:
        fixture_mae_bootstrap, fixture_rmse_bootstrap = cluster_bootstrap(
            paired_rows,
            cluster_key=lambda row: row.fixture_id,
            cluster_label="fixture_id",
            resamples=resamples,
            seed=seed,
        )

    gameweek_clusters = len({row.gameweek for row in paired_rows})

    return PairedUncertaintyResult(
        mae_point_estimate=mae_bootstrap.point_estimate,
        rmse_point_estimate=rmse_bootstrap.point_estimate,
        mae_bootstrap=mae_bootstrap,
        rmse_bootstrap=rmse_bootstrap,
        fixture_cluster_mae_bootstrap=fixture_mae_bootstrap,
        fixture_cluster_rmse_bootstrap=fixture_rmse_bootstrap,
        paired_rows=len(paired_rows),
        clusters=gameweek_clusters,
    )


def interpret_paired_verdict(
    mae_bootstrap: BootstrapResult, rmse_bootstrap: BootstrapResult
) -> tuple[VerdictState, str]:
    """Classify a paired MAE/RMSE bootstrap result into an explicit, direction-aware state.

    A CI excluding zero is not, by itself, evidence of a model *advantage* --
    an interval entirely below zero excludes zero just as validly and means
    the opposite (naive beats model). This function distinguishes the three
    possible states rather than collapsing "excludes zero" into "advantage":

    - ``"advantage"``: both MAE and RMSE CIs lie entirely above zero
      (``ci_low > 0``) -- the model's advantage is stable in this sample.
    - ``"disadvantage"``: both CIs lie entirely below zero (``ci_high < 0``)
      -- the naive comparator's advantage is stable in this sample.
    - ``"mixed"``: any other combination, including either metric's CI
      straddling zero, or the two metrics disagreeing on direction -- the
      result is not confirmed either way.

    Returns ``(state, verdict_sentence)``. The sentence is a plain-language
    research heuristic, not a formal hypothesis-test p-value.
    """
    mae_positive = mae_bootstrap.ci_low > 0.0
    mae_negative = mae_bootstrap.ci_high < 0.0
    rmse_positive = rmse_bootstrap.ci_low > 0.0
    rmse_negative = rmse_bootstrap.ci_high < 0.0

    if mae_positive and rmse_positive:
        return (
            "advantage",
            "The model's MAE and RMSE advantage over the matched-naive comparator is stable "
            "under the gameweek-cluster bootstrap: both 95% percentile intervals lie entirely "
            "above zero in this sample. This is a research heuristic, not a formal "
            "hypothesis-test p-value.",
        )
    if mae_negative and rmse_negative:
        return (
            "disadvantage",
            "The matched-naive comparator's MAE and RMSE advantage over the model is stable "
            "under the gameweek-cluster bootstrap: both 95% percentile intervals lie entirely "
            "below zero in this sample -- the model is reliably WORSE than naive here. This is "
            "a research heuristic, not a formal hypothesis-test p-value.",
        )
    return (
        "mixed",
        "The MAE and RMSE bootstrap intervals do not agree on a stable direction in this "
        "sample (at least one interval straddles zero, or the two metrics disagree) -- treat "
        "the model-vs-naive comparison as unconfirmed pending a larger evaluation window.",
    )


def build_method_notes(*, resamples: int, seed: int) -> tuple[str, str, str, str]:
    """Build the four method-notes sentences describing this run's own bootstrap parameters.

    The final sentence must reflect the ``resamples``/``seed`` actually used
    for this run (which may differ from ``DEFAULT_RESAMPLES``/``DEFAULT_SEED``
    when overridden via CLI flags), not the module-level defaults -- a
    metadata note claiming the wrong resample count or seed would silently
    misdescribe a run that used non-default values.
    """
    return (
        "Positive improvement means naive_metric - model_metric > 0, i.e. the model beats "
        "the naive comparator; negative means naive beats the model. verdict_state is "
        'direction-aware: "advantage" requires both CIs entirely above zero, '
        '"disadvantage" requires both entirely below zero, anything else is "mixed".',
        "RMSE improvement is not a per-row/per-cluster-additive quantity (RMSE is "
        "sqrt(mean(error^2))); it is recomputed on each resample's fully pooled rows, "
        "never averaged per-cluster.",
        "Primary clustering unit is the gameweek: every row scored at one gameweek's "
        "deadline shares that gameweek's causally re-derived team-strength/rate/appearance "
        "inputs, so rows within a gameweek are not independent draws. A row-level bootstrap "
        "would understate this uncertainty.",
        f"Percentile bootstrap (2.5th/97.5th percentile of {resamples} resamples, fixed "
        f"seed {seed} for this run -- defaults are {DEFAULT_RESAMPLES}/{DEFAULT_SEED}, "
        "overridable via --resamples/--seed) -- simplest, auditable, no scipy dependency; "
        "not a bias-corrected/accelerated (BCa) interval.",
    )
