"""Goal and assist components for the explainable baseline xPts model.

The rate stage follows the live Benchwarmers xG/xA pipeline: convert long- and
short-form totals to per-start-equivalent rates, blend the windows, then apply
opponent defensive strength and FPL scoring. Appearance exposure is applied in
a separate step so absences are not accidentally treated as substitute cameos.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from fpl_model.model.appearance import AppearanceProjection

REGULATION_MINUTES = 90.0
ASSIST_POINTS = 3.0
GOAL_POINTS_BY_POSITION = {
    "GK": 10.0,
    "DEF": 6.0,
    "MID": 5.0,
    "FWD": 4.0,
}


def _validate_probability(value: float, name: str) -> None:
    if not isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be finite and between 0 and 1")


def _validate_non_negative(value: float, name: str) -> None:
    if not isfinite(value) or value < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class AttackingWindow:
    """Minutes and expected contributions observed in one historical window."""

    minutes: float
    expected_goals: float
    expected_assists: float

    def __post_init__(self) -> None:
        _validate_non_negative(self.minutes, "minutes")
        _validate_non_negative(self.expected_goals, "expected_goals")
        _validate_non_negative(self.expected_assists, "expected_assists")
        if self.minutes == 0.0 and (
            self.expected_goals > 0.0 or self.expected_assists > 0.0
        ):
            raise ValueError("non-zero xG/xA requires positive minutes")


@dataclass(frozen=True, slots=True)
class AttackingRateProjection:
    """Fixture-adjusted goal and assist expectations conditional on starting."""

    long_form_xg_per_start: float
    short_form_xg_per_start: float
    long_form_xa_per_start: float
    short_form_xa_per_start: float
    blended_xg_per_start: float
    blended_xa_per_start: float
    goal_xpts_if_start: float
    assist_xpts_if_start: float


@dataclass(frozen=True, slots=True)
class AttackingProjection:
    """Unconditional goal and assist xPts after appearance exposure."""

    rate_projection: AttackingRateProjection
    attacking_exposure: float
    goal_xpts: float
    assist_xpts: float
    total_xpts: float


def _per_start_rate(
    contribution: float,
    minutes: float,
    minutes_per_start_fraction: float,
) -> float:
    if minutes == 0.0:
        return 0.0
    return contribution / minutes * REGULATION_MINUTES * minutes_per_start_fraction


def project_benchwarmers_attacking_rates(
    long_form: AttackingWindow,
    short_form: AttackingWindow,
    *,
    minutes_per_start_fraction: float,
    position: str,
    opponent_defensive_multiplier: float,
    long_form_weight: float = 0.8,
    assist_boost: float = 0.4,
) -> AttackingRateProjection:
    """Reproduce the live workbook goal/assist rate path without no-op scaling.

    The workbook multiplies blended rates by points on ``MODEL`` and divides
    them back out on ``PPts``. This implementation keeps the raw rates until the
    final scoring step, which is algebraically identical and easier to audit.
    Home/away adjustment and appearance probabilities are intentionally absent.
    """
    _validate_probability(minutes_per_start_fraction, "minutes_per_start_fraction")
    _validate_probability(long_form_weight, "long_form_weight")
    _validate_non_negative(
        opponent_defensive_multiplier,
        "opponent_defensive_multiplier",
    )
    _validate_non_negative(assist_boost, "assist_boost")
    try:
        goal_points = GOAL_POINTS_BY_POSITION[position]
    except KeyError as error:
        valid_positions = ", ".join(GOAL_POINTS_BY_POSITION)
        raise ValueError(f"position must be one of: {valid_positions}") from error

    short_form_weight = 1.0 - long_form_weight
    long_form_xg_per_start = _per_start_rate(
        long_form.expected_goals,
        long_form.minutes,
        minutes_per_start_fraction,
    )
    short_form_xg_per_start = _per_start_rate(
        short_form.expected_goals,
        short_form.minutes,
        minutes_per_start_fraction,
    )
    long_form_xa_per_start = _per_start_rate(
        long_form.expected_assists,
        long_form.minutes,
        minutes_per_start_fraction,
    )
    short_form_xa_per_start = _per_start_rate(
        short_form.expected_assists,
        short_form.minutes,
        minutes_per_start_fraction,
    )

    blended_xg_per_start = (
        long_form_weight * long_form_xg_per_start
        + short_form_weight * short_form_xg_per_start
    )
    blended_xa_per_start = (
        long_form_weight * long_form_xa_per_start
        + short_form_weight * short_form_xa_per_start
    )
    goal_xpts_if_start = (
        blended_xg_per_start * opponent_defensive_multiplier * goal_points
    )
    assist_xpts_if_start = (
        blended_xa_per_start
        * opponent_defensive_multiplier
        * (1.0 + assist_boost)
        * ASSIST_POINTS
    )

    return AttackingRateProjection(
        long_form_xg_per_start=float(long_form_xg_per_start),
        short_form_xg_per_start=float(short_form_xg_per_start),
        long_form_xa_per_start=float(long_form_xa_per_start),
        short_form_xa_per_start=float(short_form_xa_per_start),
        blended_xg_per_start=float(blended_xg_per_start),
        blended_xa_per_start=float(blended_xa_per_start),
        goal_xpts_if_start=float(goal_xpts_if_start),
        assist_xpts_if_start=float(assist_xpts_if_start),
    )


def weight_attacking_rates(
    rate_projection: AttackingRateProjection,
    appearance: AppearanceProjection,
    *,
    substitute_to_start_minutes_ratio: float,
) -> AttackingProjection:
    """Apply mutually exclusive start and substitute-appearance exposure.

    The source workbook weights its cameo branch with ``1 - P(start)``, which
    includes genuine absences. The coherent baseline instead uses the explicit
    substitute-appearance probability produced by the appearance component.
    """
    _validate_non_negative(
        substitute_to_start_minutes_ratio,
        "substitute_to_start_minutes_ratio",
    )
    attacking_exposure = (
        appearance.start_probability
        + appearance.substitute_appearance_probability
        * substitute_to_start_minutes_ratio
    )
    goal_xpts = rate_projection.goal_xpts_if_start * attacking_exposure
    assist_xpts = rate_projection.assist_xpts_if_start * attacking_exposure

    return AttackingProjection(
        rate_projection=rate_projection,
        attacking_exposure=float(attacking_exposure),
        goal_xpts=float(goal_xpts),
        assist_xpts=float(assist_xpts),
        total_xpts=float(goal_xpts + assist_xpts),
    )
