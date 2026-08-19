"""Walk-forward calibration of the appearance model's own predictions.

The 11-component xPts model's walk-forward calibration (``validation.
walk_forward_calibration``) found the highest predicted-xPts band is
materially overconfident. `start_probability`/`expected_minutes`
(``appearance_as_of``) and the per-90 rate components built on top of them
(goals/assists/bonus/etc.) are highly correlated in that band -- a nailed-on
starter has high values on both -- so that finding alone cannot say whether
the overprediction traces back to the appearance model itself, the rate
components, or both.

This module isolates the appearance model: for each of ``start_probability``
(predicted against realised ``actual_started``) and ``expected_minutes``
(predicted against realised ``actual_minutes``), it fits OLS
``actual ~ predicted`` using ONLY strictly earlier gameweeks at each
evaluation step -- the same no-lookahead boundary
``validation.walk_forward_calibration`` already applies to xPts itself, and
which ``appearance_as_of`` already applies to the appearance predictions
themselves. This calibration is a *second*, independent no-lookahead layer:
not just "was `start_probability`/`expected_minutes` computed causally" but
"was the calibration of that prediction's own accuracy also fit only on
prior gameweeks."

The closed-form OLS math is deliberately duplicated (not imported) from
``validation.walk_forward_calibration``, matching this codebase's per-module
convention of small local duplication over cross-module coupling for
validation code -- this module stays independently runnable/testable.

Read-only measurement. Does not change any xPts component or formula, and is
not applied to any production projection path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from fpl_model.validation.backtest import BacktestObservation
from fpl_model.validation.benchwarmers_backtest import AppearanceObservation

DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS = 5

AppearanceCalibrationTarget = Literal["start_probability", "expected_minutes"]

AppearanceImplicationVerdict = Literal["both", "start_probability_only", "expected_minutes_only", "neither"]


@dataclass(frozen=True, slots=True)
class OlsFit:
    """OLS ``actual_value ~ predicted_value``, fit on some training row set."""

    slope: float
    intercept: float
    training_rows: int
    training_gameweeks: int


@dataclass(frozen=True, slots=True)
class AppearanceCalibrationRow:
    """One player-fixture's appearance prediction/outcome pair for one target.

    ``predicted_value``/``actual_value`` hold ``start_probability``/
    ``actual_started`` (as 0.0/1.0) or ``expected_minutes``/``actual_minutes``,
    depending on which target this row was built for -- never both at once
    (see ``walk_forward_appearance_calibration``'s ``target`` parameter).

    Shares the ``.gameweek`` attribute
    ``validation.paired_uncertainty.block_bootstrap_statistic`` requires, so
    pooled out-of-sample rows can be bootstrapped with that same primitive.
    """

    gameweek: int
    player_id: int | str
    fixture_id: int | str
    predicted_value: float
    actual_value: float


@dataclass(frozen=True, slots=True)
class GameweekAppearanceCalibrationRecord:
    """One evaluation gameweek's out-of-sample walk-forward calibration result."""

    gameweek: int
    target: AppearanceCalibrationTarget
    fit: OlsFit
    evaluation_rows: tuple[AppearanceCalibrationRow, ...]


def fit_ols(
    predicted_values: Sequence[float],
    actual_values: Sequence[float],
    *,
    training_gameweeks: int = 1,
) -> OlsFit:
    """Closed-form OLS slope/intercept of ``actual_values`` on ``predicted_values``.

    ``slope = Sxy / Sxx``, ``intercept = y_bar - slope * x_bar`` where ``Sxx``
    is the sum of squared deviations of ``predicted_values`` from its mean
    and ``Sxy`` is the corresponding cross-deviation sum. Raises
    ``ValueError`` if fewer than 2 rows are given or ``predicted_values`` is
    constant (``Sxx == 0.0``, slope undefined).

    For ``target="start_probability"``, ``actual_values`` are 0.0/1.0
    (realised started/not-started) -- OLS on a binary outcome is a linear
    probability model: slope < 1 means the projection is systematically
    overconfident (extreme probabilities pulled toward the outcome less than
    1:1), the same plain-language interpretation as the xPts calibration
    slope. This mirrors this codebase's closed-form, no scipy/sklearn
    convention rather than introducing logistic regression or a Brier-score
    decomposition with no precedent here.
    """
    if len(predicted_values) != len(actual_values):
        raise ValueError("predicted_values and actual_values must be the same length")
    if len(predicted_values) < 2:
        raise ValueError("at least 2 rows are required to fit a slope")
    if training_gameweeks < 1:
        raise ValueError("training_gameweeks must be a positive integer")

    x = np.asarray(predicted_values, dtype=np.float64)
    y = np.asarray(actual_values, dtype=np.float64)
    x_bar = float(np.mean(x))
    y_bar = float(np.mean(y))
    sxx = float(np.sum((x - x_bar) ** 2))
    if sxx == 0.0:
        raise ValueError("predicted_values is constant across rows; slope is undefined")
    sxy = float(np.sum((x - x_bar) * (y - y_bar)))
    slope = sxy / sxx
    intercept = y_bar - slope * x_bar
    return OlsFit(
        slope=slope,
        intercept=intercept,
        training_rows=len(predicted_values),
        training_gameweeks=training_gameweeks,
    )


def pooled_ols_from_rows(rows: Sequence[AppearanceCalibrationRow]) -> OlsFit:
    """Fit OLS on ``rows``, deriving ``training_gameweeks`` from their own gameweek tags."""
    return fit_ols(
        [row.predicted_value for row in rows],
        [row.actual_value for row in rows],
        training_gameweeks=len({row.gameweek for row in rows}),
    )


def apply_calibration(
    rows: Sequence[AppearanceCalibrationRow], fit: OlsFit
) -> tuple[float, ...]:
    """Return ``intercept + slope * predicted_value`` for each row in ``rows``."""
    return tuple(fit.intercept + fit.slope * row.predicted_value for row in rows)


def _observations_to_rows(
    observations: Sequence[AppearanceObservation], *, target: AppearanceCalibrationTarget
) -> tuple[AppearanceCalibrationRow, ...]:
    if target == "start_probability":
        return tuple(
            AppearanceCalibrationRow(
                gameweek=observation.gameweek,
                player_id=observation.player_code,
                fixture_id=observation.fixture_id,
                predicted_value=observation.predicted_start_probability,
                actual_value=1.0 if observation.actual_started else 0.0,
            )
            for observation in observations
        )
    if target == "expected_minutes":
        return tuple(
            AppearanceCalibrationRow(
                gameweek=observation.gameweek,
                player_id=observation.player_code,
                fixture_id=observation.fixture_id,
                predicted_value=observation.predicted_expected_minutes,
                actual_value=observation.actual_minutes,
            )
            for observation in observations
        )
    raise ValueError(f'target must be "start_probability" or "expected_minutes", got {target!r}')


def walk_forward_appearance_calibration(
    observations: Sequence[AppearanceObservation],
    *,
    target: AppearanceCalibrationTarget,
    minimum_calibration_gameweeks: int = DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS,
) -> tuple[GameweekAppearanceCalibrationRecord, ...]:
    """Build one out-of-sample calibration record per eligible evaluation gameweek.

    A gameweek ``G`` is eligible once at least ``minimum_calibration_gameweeks``
    distinct earlier gameweeks are present in ``observations``. At each
    eligible step, ``fit`` is fit on every row from gameweeks strictly before
    ``G`` for the chosen ``target``; ``G``'s own rows are used only as the
    returned evaluation rows, never to fit the model.

    No high-prediction-band split (unlike the xPts calibration):
    ``start_probability`` is already bounded [0,1] and its own "high band"
    (nailed-on starters) is precisely what this overall calibration already
    targets, so a second band split would multiply scope without probing a
    question the xPts calibration's own high band didn't already ask.
    """
    if not observations:
        raise ValueError("at least one observation is required")
    if minimum_calibration_gameweeks < 1:
        raise ValueError("minimum_calibration_gameweeks must be a positive integer")

    rows = _observations_to_rows(observations, target=target)
    rows_by_gameweek: dict[int, list[AppearanceCalibrationRow]] = {}
    for row in rows:
        rows_by_gameweek.setdefault(row.gameweek, []).append(row)
    distinct_gameweeks = sorted(rows_by_gameweek)

    records: list[GameweekAppearanceCalibrationRecord] = []
    for index, gameweek in enumerate(distinct_gameweeks):
        prior_gameweeks = distinct_gameweeks[:index]
        if len(prior_gameweeks) < minimum_calibration_gameweeks:
            continue

        prior_rows: list[AppearanceCalibrationRow] = []
        for prior_gameweek in prior_gameweeks:
            prior_rows.extend(rows_by_gameweek[prior_gameweek])
        evaluation_rows = tuple(rows_by_gameweek[gameweek])

        fit = pooled_ols_from_rows(prior_rows)

        records.append(
            GameweekAppearanceCalibrationRecord(
                gameweek=gameweek,
                target=target,
                fit=fit,
                evaluation_rows=evaluation_rows,
            )
        )

    return tuple(records)


def pooled_walk_forward_slope(
    records: Sequence[GameweekAppearanceCalibrationRecord],
) -> dict[str, object]:
    """Pool every eligible record's own out-of-sample evaluation rows into one final OLS fit.

    This is a *second*, separate regression from each record's own ``fit``
    (which is fit on prior data): it fits directly on the concatenation of
    what was actually scored out-of-sample at each walk-forward step,
    answering "how calibrated were the walk-forward predictions actually
    applied, in aggregate" -- not "what was the average of the per-step
    fitted slopes."

    Descriptive-only: NEVER used to produce calibrated predictions (that
    would be in-sample with respect to this pooled fit, exactly the bug
    fixed in the xPts calibration's own pooled fit -- see
    ``causal_walk_forward_predictions`` for the causal prediction source).
    """
    if not records:
        raise ValueError("at least one calibration record is required")

    pooled_rows = tuple(row for record in records for row in record.evaluation_rows)
    if len(pooled_rows) < 2:
        raise ValueError("fewer than 2 pooled evaluation rows available; cannot fit a pooled slope")

    fit = pooled_ols_from_rows(pooled_rows)
    return {
        "target": records[0].target,
        "slope": fit.slope,
        "intercept": fit.intercept,
        "evaluation_rows": len(pooled_rows),
        "eligible_gameweeks": len(records),
        "rows": pooled_rows,
    }


def causal_walk_forward_predictions(
    records: Sequence[GameweekAppearanceCalibrationRecord],
) -> tuple[tuple[AppearanceCalibrationRow, ...], tuple[float, ...]]:
    """Apply each record's own prior-gameweeks-only fit to that record's evaluation rows.

    This is the causal walk-forward calibrated prediction: gameweek ``G``'s
    evaluation rows are calibrated using ``record.fit``, which was fit only
    on strictly-prior gameweeks -- never a fit derived from ``G``'s own or
    any later gameweek's outcomes, and never ``pooled_walk_forward_slope``'s
    pooled fit (which is itself fit across every eligible gameweek's
    evaluation rows and would leak each row's own eventual outcome back into
    its own calibration if used to produce predictions).

    Returns ``(rows, calibrated_predictions)`` with ``calibrated_predictions[i]``
    corresponding to ``rows[i]``, in the same gameweek order ``records`` is given in.
    """
    rows: list[AppearanceCalibrationRow] = []
    calibrated_predictions: list[float] = []
    for record in records:
        if not record.evaluation_rows:
            continue
        rows.extend(record.evaluation_rows)
        calibrated_predictions.extend(apply_calibration(record.evaluation_rows, record.fit))
    return tuple(rows), tuple(calibrated_predictions)


def xpts_keys_from_backtest_observations(
    observations: Sequence[BacktestObservation],
) -> frozenset[tuple[int | str, int | str, int]]:
    """Return the ``(player_id, fixture_id, gameweek)`` key set scored by the xPts backtest.

    The sole intended use is deriving ``xpts_scored_aligned_observations``'s
    ``xpts_keys`` argument directly from ``BacktestObservation``, never from
    re-running or coupling to the xPts scoring loop's internal gap logic.
    """
    return frozenset((o.player_id, o.fixture_id, o.gameweek) for o in observations)


def xpts_scored_aligned_observations(
    appearance_observations: Sequence[AppearanceObservation],
    xpts_keys: frozenset[tuple[int | str, int | str, int]],
) -> tuple[AppearanceObservation, ...]:
    """Restrict ``appearance_observations`` to keys that also produced a ``BacktestObservation``.

    This is the sensitivity cohort, not the primary one:
    ``appearance_observations`` itself (``appearance_eligible``) is gated
    only on appearance-model inputs (non-DGW + a causal appearance
    prediction), never on the unrelated xPts requirements (player rates,
    team strength, team IDs, league-average bonus rates) -- conditioning
    appearance-model calibration on xPts scoring success would bias the
    calibration sample toward players/fixtures with complete xPts inputs.
    ``xpts_keys`` (build via ``xpts_keys_from_backtest_observations``) is
    always a subset check against ``BacktestObservation``'s own keys, so the
    returned cohort is always a subset of ``appearance_observations`` (never
    larger).
    """
    return tuple(
        observation
        for observation in appearance_observations
        if (observation.player_code, observation.fixture_id, observation.gameweek) in xpts_keys
    )


def appearance_implication_verdict(
    *, start_probability_ci_below_one: bool, expected_minutes_ci_below_one: bool
) -> AppearanceImplicationVerdict:
    """Classify which target(s)' slope bootstrap CI lies entirely below 1.0.

    Four mutually exclusive states, direction-aware so the caller can never
    describe a single-target result as though both targets agreed:

    - ``"both"``: both targets' CIs lie entirely below 1.0.
    - ``"start_probability_only"``: only ``start_probability``'s CI does.
    - ``"expected_minutes_only"``: only ``expected_minutes``'s CI does.
    - ``"neither"``: neither target's CI lies entirely below 1.0.

    Deliberately takes two named booleans (each caller-computed as
    ``result["slope_bootstrap"]["ci_high"] < 1.0``) rather than an OR of the
    two -- an OR collapses the mixed cases into the same boolean as "both",
    which previously produced an interpretation sentence claiming *both*
    targets' CIs lie below 1.0 even when only one did.
    """
    if start_probability_ci_below_one and expected_minutes_ci_below_one:
        return "both"
    if start_probability_ci_below_one:
        return "start_probability_only"
    if expected_minutes_ci_below_one:
        return "expected_minutes_only"
    return "neither"


def describe_appearance_implication_verdict(verdict: AppearanceImplicationVerdict) -> str:
    """Return interpretation wording that matches ``verdict`` exactly.

    Each branch's claim is scoped to precisely the target(s) named by
    ``verdict`` -- never "both" wording for a single-target result, and never
    silently dropping the mixed-evidence case into an unqualified "yes" or
    "no".
    """
    if verdict == "both":
        return (
            "Both targets' slope bootstrap CIs lie entirely below 1.0 in the primary "
            "appearance_eligible cohort, consistent with the appearance model "
            "(start_probability/expected_minutes) being a material contributor to the xPts "
            "calibration's high-band overprediction finding -- though the rate components "
            "built on top of it may also independently contribute; this script cannot rule "
            "that out. A slope below 1.0 means predictions are too extreme, not necessarily "
            "too high; see each target's mean_bias for the separate over/under-prediction "
            "claim."
        )
    if verdict == "start_probability_only":
        return (
            "Only start_probability's slope bootstrap CI lies entirely below 1.0 in the "
            "primary appearance_eligible cohort; expected_minutes' CI does not. This "
            "implicates start_probability specifically, not the appearance model as a whole "
            "-- expected_minutes calibration is not a supported explanation for the xPts "
            "calibration's high-band overprediction finding, though the per-90 rate "
            "components remain a possible additional contributor this script cannot rule out."
        )
    if verdict == "expected_minutes_only":
        return (
            "Only expected_minutes' slope bootstrap CI lies entirely below 1.0 in the "
            "primary appearance_eligible cohort; start_probability's CI does not. This "
            "implicates expected_minutes specifically, not the appearance model as a whole "
            "-- start_probability calibration is not a supported explanation for the xPts "
            "calibration's high-band overprediction finding, though the per-90 rate "
            "components remain a possible additional contributor this script cannot rule out."
        )
    return (
        "Neither target's slope bootstrap CI lies entirely below 1.0 in the primary "
        "appearance_eligible cohort, which does NOT support the appearance model as the "
        "primary source of the xPts calibration's high-band overprediction -- the per-90 "
        "rate components (goals/assists/bonus/etc., conditional on starting) are a more "
        "likely remaining explanation."
    )
