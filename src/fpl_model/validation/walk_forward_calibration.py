"""Walk-forward calibration slope/intercept of actual_points on predicted_xpts.

For each evaluation gameweek ``G``, fits OLS ``actual_points ~ predicted_xpts``
using ONLY observations from gameweeks strictly before ``G`` -- the same
no-lookahead boundary every other backtest input in this codebase already
enforces (``player_rates_as_of``/``appearance_as_of``/``team_strength_as_of``'s
``target_deadline`` gate, ``walk_forward_folds``'s training/evaluation split).
This measures what calibration would have been achievable, in-season, at each
deadline; it is not a full-sample regression, which would leak future
gameweeks into an early gameweek's diagnostic.

Read-only measurement. Does not change any xPts component or formula, and is
not applied to any production projection path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from fpl_model.validation.backtest import BacktestObservation

DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS = 5
DEFAULT_HIGH_BAND_PERCENTILE = 75.0


@dataclass(frozen=True, slots=True)
class OlsFit:
    """OLS ``actual_points ~ predicted_xpts``, fit on some training row set."""

    slope: float
    intercept: float
    training_rows: int
    training_gameweeks: int


@dataclass(frozen=True, slots=True)
class CalibrationRow:
    """One player-fixture's prediction/outcome pair, tagged with its gameweek.

    Shares the ``.gameweek`` attribute
    ``validation.paired_uncertainty.block_bootstrap_statistic`` requires, so
    pooled out-of-sample rows can be bootstrapped with that same primitive
    rather than a second bespoke resampling implementation.
    """

    gameweek: int
    player_id: int | str
    fixture_id: int | str
    predicted_xpts: float
    actual_points: float


@dataclass(frozen=True, slots=True)
class GameweekCalibrationRecord:
    """One evaluation gameweek's out-of-sample walk-forward calibration result."""

    gameweek: int
    overall_fit: OlsFit
    overall_evaluation_rows: tuple[CalibrationRow, ...]
    high_band_threshold: float
    high_band_fit: OlsFit | None
    high_band_evaluation_rows: tuple[CalibrationRow, ...]


def fit_ols(
    predicted_xpts: Sequence[float],
    actual_points: Sequence[float],
    *,
    training_gameweeks: int = 1,
) -> OlsFit:
    """Closed-form OLS slope/intercept of ``actual_points`` on ``predicted_xpts``.

    ``slope = Sxy / Sxx``, ``intercept = y_bar - slope * x_bar`` where ``Sxx``
    is the sum of squared deviations of ``predicted_xpts`` from its mean and
    ``Sxy`` is the corresponding cross-deviation sum. Raises ``ValueError`` if
    fewer than 2 rows are given or ``predicted_xpts`` is constant (``Sxx ==
    0.0``, slope undefined).

    ``training_gameweeks`` is a pass-through label (this function has no
    notion of gameweeks -- it only sees plain value sequences); callers that
    know how many distinct gameweeks contributed the training rows (e.g.
    ``pooled_ols_from_rows``) should supply it so ``OlsFit.training_gameweeks``
    is accurate. It defaults to 1 for direct callers that don't track this.
    """
    if len(predicted_xpts) != len(actual_points):
        raise ValueError("predicted_xpts and actual_points must be the same length")
    if len(predicted_xpts) < 2:
        raise ValueError("at least 2 rows are required to fit a slope")
    if training_gameweeks < 1:
        raise ValueError("training_gameweeks must be a positive integer")

    x = np.asarray(predicted_xpts, dtype=np.float64)
    y = np.asarray(actual_points, dtype=np.float64)
    x_bar = float(np.mean(x))
    y_bar = float(np.mean(y))
    sxx = float(np.sum((x - x_bar) ** 2))
    if sxx == 0.0:
        raise ValueError("predicted_xpts is constant across rows; slope is undefined")
    sxy = float(np.sum((x - x_bar) * (y - y_bar)))
    slope = sxy / sxx
    intercept = y_bar - slope * x_bar
    return OlsFit(
        slope=slope,
        intercept=intercept,
        training_rows=len(predicted_xpts),
        training_gameweeks=training_gameweeks,
    )


def pooled_ols_from_rows(rows: Sequence[CalibrationRow]) -> OlsFit:
    """Fit OLS on ``rows``, deriving ``training_gameweeks`` from their own gameweek tags."""
    return fit_ols(
        [row.predicted_xpts for row in rows],
        [row.actual_points for row in rows],
        training_gameweeks=len({row.gameweek for row in rows}),
    )


def apply_calibration(rows: Sequence[CalibrationRow], fit: OlsFit) -> tuple[float, ...]:
    """Return ``intercept + slope * predicted_xpts`` for each row in ``rows``."""
    return tuple(fit.intercept + fit.slope * row.predicted_xpts for row in rows)


def _observations_to_rows(observations: Sequence[BacktestObservation]) -> tuple[CalibrationRow, ...]:
    return tuple(
        CalibrationRow(
            gameweek=observation.gameweek,
            player_id=observation.player_id,
            fixture_id=observation.fixture_id,
            predicted_xpts=observation.predicted_xpts,
            actual_points=observation.actual_points,
        )
        for observation in observations
    )


def walk_forward_calibration(
    observations: Sequence[BacktestObservation],
    *,
    minimum_calibration_gameweeks: int = DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS,
    high_band_percentile: float = DEFAULT_HIGH_BAND_PERCENTILE,
) -> tuple[GameweekCalibrationRecord, ...]:
    """Build one out-of-sample calibration record per eligible evaluation gameweek.

    A gameweek ``G`` is eligible once at least ``minimum_calibration_gameweeks``
    distinct earlier gameweeks are present in ``observations``. At each
    eligible step: fit ``overall_fit`` on every row from gameweeks strictly
    before ``G``; separately compute ``high_band_threshold`` as the
    ``high_band_percentile``-th percentile of that same prior pool's
    ``predicted_xpts`` (never including ``G``'s own or any later gameweek's
    rows -- reusing the full-season quartile boundaries a read-only segment
    diagnostic might use would leak future gameweeks' distribution into an
    early gameweek's "high" definition), restrict the prior pool to rows at
    or above that threshold, and fit ``high_band_fit`` on the restricted
    pool -- or leave it ``None`` if fewer than 2 restricted training rows
    remain, or if those rows have degenerate (constant) ``predicted_xpts``
    and cannot support a slope (some early eligible gameweeks may have a
    thin or non-varying high band; this is not an error). ``G``'s own rows
    are used only as the returned evaluation rows, never to fit either
    model.
    """
    if not observations:
        raise ValueError("at least one observation is required")
    if minimum_calibration_gameweeks < 1:
        raise ValueError("minimum_calibration_gameweeks must be a positive integer")
    if not 0.0 < high_band_percentile < 100.0:
        raise ValueError("high_band_percentile must be between 0 and 100 (exclusive)")

    rows = _observations_to_rows(observations)
    rows_by_gameweek: dict[int, list[CalibrationRow]] = {}
    for row in rows:
        rows_by_gameweek.setdefault(row.gameweek, []).append(row)
    distinct_gameweeks = sorted(rows_by_gameweek)

    records: list[GameweekCalibrationRecord] = []
    for index, gameweek in enumerate(distinct_gameweeks):
        prior_gameweeks = distinct_gameweeks[:index]
        if len(prior_gameweeks) < minimum_calibration_gameweeks:
            continue

        prior_rows: list[CalibrationRow] = []
        for prior_gameweek in prior_gameweeks:
            prior_rows.extend(rows_by_gameweek[prior_gameweek])
        evaluation_rows = tuple(rows_by_gameweek[gameweek])

        overall_fit = pooled_ols_from_rows(prior_rows)

        prior_predicted = np.asarray([row.predicted_xpts for row in prior_rows], dtype=np.float64)
        high_band_threshold = float(np.percentile(prior_predicted, high_band_percentile))
        prior_high_band_rows = [
            row for row in prior_rows if row.predicted_xpts >= high_band_threshold
        ]
        high_band_fit: OlsFit | None = None
        if len(prior_high_band_rows) >= 2:
            try:
                high_band_fit = pooled_ols_from_rows(prior_high_band_rows)
            except ValueError:
                # A too-thin or degenerate (constant predicted_xpts) prior
                # high band cannot support a slope -- treated the same as
                # too-few-rows: omitted, not fabricated or raised.
                high_band_fit = None
        high_band_evaluation_rows = tuple(
            row for row in evaluation_rows if row.predicted_xpts >= high_band_threshold
        )

        records.append(
            GameweekCalibrationRecord(
                gameweek=gameweek,
                overall_fit=overall_fit,
                overall_evaluation_rows=evaluation_rows,
                high_band_threshold=high_band_threshold,
                high_band_fit=high_band_fit,
                high_band_evaluation_rows=high_band_evaluation_rows,
            )
        )

    return tuple(records)


def pooled_walk_forward_slope(
    records: Sequence[GameweekCalibrationRecord],
    *,
    band: Literal["overall", "high"] = "overall",
) -> dict[str, object]:
    """Pool every eligible record's own out-of-sample evaluation rows into one final OLS fit.

    This is a *second*, separate regression from each record's own
    ``overall_fit``/``high_band_fit`` (which are fit on prior data): it fits
    directly on the concatenation of what was actually scored out-of-sample
    at each walk-forward step, answering "how calibrated were the walk-forward
    predictions actually applied, in aggregate" -- not "what was the average
    of the per-step fitted slopes."

    ``band="high"`` pools only each eligible record's own
    ``high_band_evaluation_rows``, skipping any record whose
    ``high_band_fit is None`` (no high-band training data was available at
    that step, so that step's evaluation rows cannot be attributed to a
    walk-forward high-band decision).
    """
    if not records:
        raise ValueError("at least one calibration record is required")

    if band == "overall":
        pooled_rows = tuple(row for record in records for row in record.overall_evaluation_rows)
        eligible_gameweeks = len(records)
    elif band == "high":
        eligible_records = [record for record in records if record.high_band_fit is not None]
        pooled_rows = tuple(
            row for record in eligible_records for row in record.high_band_evaluation_rows
        )
        eligible_gameweeks = len(eligible_records)
    else:
        raise ValueError(f'band must be "overall" or "high", got {band!r}')

    if len(pooled_rows) < 2:
        raise ValueError(
            f"fewer than 2 pooled evaluation rows available for band={band!r}; "
            "cannot fit a pooled slope"
        )

    fit = pooled_ols_from_rows(pooled_rows)
    return {
        "band": band,
        "slope": fit.slope,
        "intercept": fit.intercept,
        "evaluation_rows": len(pooled_rows),
        "eligible_gameweeks": eligible_gameweeks,
        "rows": pooled_rows,
    }


def causal_walk_forward_predictions(
    records: Sequence[GameweekCalibrationRecord],
    *,
    band: Literal["overall", "high"] = "overall",
) -> tuple[tuple[CalibrationRow, ...], tuple[float, ...]]:
    """Apply each record's own prior-gameweeks-only fit to that record's evaluation rows.

    This produces the CAUSAL walk-forward calibrated prediction: gameweek
    ``G``'s evaluation rows are calibrated using ``record.overall_fit`` (or
    ``record.high_band_fit`` for ``band="high"``), which -- per
    ``walk_forward_calibration`` -- was fit only on strictly-prior gameweeks.
    Current or future gameweeks' outcomes can never change an earlier
    gameweek's calibrated prediction, because each record's fit was already
    frozen before that gameweek's evaluation rows existed.

    Never use ``pooled_walk_forward_slope``'s pooled fit to produce
    predictions instead of this function: that fit is derived from every
    eligible gameweek's own evaluation rows pooled together, so applying it
    back to those same rows would let each row's own eventual outcome leak
    into its own calibrated prediction (in-sample with respect to the pooled
    fit). ``pooled_walk_forward_slope`` remains valid as a *descriptive*
    summary of the walk-forward slope trajectory; it is simply not a source
    of causal predictions.

    ``band="high"`` skips any record whose ``high_band_fit is None`` (no
    prior high-band training data at that step) or whose
    ``high_band_evaluation_rows`` is empty -- no causal high-band prediction
    can be made for such a record. ``band="overall"`` never skips a record,
    since ``overall_fit`` is always present once a record exists.

    Returns ``(rows, calibrated_predictions)`` with ``calibrated_predictions[i]``
    corresponding to ``rows[i]``, in the same gameweek order ``records`` is
    given in (rows within one gameweek keep their original relative order).
    """
    rows: list[CalibrationRow] = []
    calibrated_predictions: list[float] = []
    for record in records:
        if band == "overall":
            fit = record.overall_fit
            evaluation_rows = record.overall_evaluation_rows
        elif band == "high":
            fit = record.high_band_fit
            evaluation_rows = record.high_band_evaluation_rows
        else:
            raise ValueError(f'band must be "overall" or "high", got {band!r}')
        if fit is None or not evaluation_rows:
            continue
        rows.extend(evaluation_rows)
        calibrated_predictions.extend(apply_calibration(evaluation_rows, fit))
    return tuple(rows), tuple(calibrated_predictions)
