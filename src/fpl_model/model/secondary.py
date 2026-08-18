"""Saves, discipline, bonus, and defensive-contribution xPts components."""

from __future__ import annotations

from dataclasses import dataclass
from math import exp, isfinite

from fpl_model.model.appearance import AppearanceProjection

SAVE_BUNDLE_SIZE = 3
YELLOW_CARD_POINTS = -1.0
RED_CARD_POINTS = -3.0
BONUS_POINTS_MAXIMUM = 3.0
DEFCON_POINTS_BY_POSITION = {
    "GK": 0.0,
    "DEF": 2.0,
    "MID": 2.0,
    "FWD": 2.0,
}
DEFCON_THRESHOLD_BY_POSITION = {
    "GK": 12,
    "DEF": 10,
    "MID": 12,
    "FWD": 12,
}


def _validate_probability(value: float, name: str) -> None:
    if not isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be finite and between 0 and 1")


def _validate_non_negative(value: float, name: str) -> None:
    if not isfinite(value) or value < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")


def _position_value(mapping: dict[str, float | int], position: str) -> float | int:
    try:
        return mapping[position]
    except KeyError as error:
        valid_positions = ", ".join(mapping)
        raise ValueError(f"position must be one of: {valid_positions}") from error


def poisson_tail_probability(poisson_lambda: float, threshold: int) -> float:
    """Return ``P(X >= threshold)`` for a Poisson random variable."""
    _validate_non_negative(poisson_lambda, "poisson_lambda")
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
        raise ValueError("threshold must be a positive integer")
    probability = exp(-poisson_lambda)
    cumulative = probability
    for count in range(1, threshold):
        probability *= poisson_lambda / count
        cumulative += probability
    return max(0.0, min(1.0, 1.0 - cumulative))


def expected_poisson_bundles(
    poisson_lambda: float,
    bundle_size: int,
    *,
    tolerance: float = 1e-12,
) -> float:
    """Return ``E[floor(X / bundle_size)]`` for a Poisson variable."""
    _validate_non_negative(poisson_lambda, "poisson_lambda")
    if isinstance(bundle_size, bool) or not isinstance(bundle_size, int) or bundle_size < 1:
        raise ValueError("bundle_size must be a positive integer")
    if not isfinite(tolerance) or tolerance <= 0.0:
        raise ValueError("tolerance must be finite and positive")
    if poisson_lambda == 0.0:
        return 0.0

    probability = exp(-poisson_lambda)
    cumulative = probability
    expected_bundles = 0.0
    for count in range(1, 10_001):
        probability *= poisson_lambda / count
        cumulative += probability
        expected_bundles += (count // bundle_size) * probability
        if count > poisson_lambda and 1.0 - cumulative <= tolerance:
            return expected_bundles
    raise ArithmeticError("Poisson bundle expectation did not converge")


@dataclass(frozen=True, slots=True)
class WeightedExpectedPoints:
    """Probability-weighted start and substitute contributions."""

    start_xpts: float
    substitute_xpts: float
    total_xpts: float


@dataclass(frozen=True, slots=True)
class SavesRateProjection:
    """Fixture-adjusted goalkeeper save expectation conditional on playing 90."""

    save_lambda_if_start: float
    workbook_xpts_if_start: float
    exact_xpts_if_start: float


@dataclass(frozen=True, slots=True)
class DisciplineRateProjection:
    """Expected card deductions conditional on starting."""

    yellow_card_rate_if_start: float
    red_card_rate_if_start: float
    yellow_card_xpts_if_start: float
    red_card_xpts_if_start: float
    workbook_red_card_xpts_if_start: float
    total_xpts_if_start: float


@dataclass(frozen=True, slots=True)
class BonusRateProjection:
    """Fixture-adjusted bonus expectation conditional on starting."""

    historical_bonus_rate_if_start: float
    fixture_multiplier: float
    workbook_uncapped_xpts_if_start: float
    bounded_xpts_if_start: float


@dataclass(frozen=True, slots=True)
class DefConRateProjection:
    """DefCon threshold probability and xPts conditional on starting."""

    long_form_lambda_if_start: float
    short_form_lambda_if_start: float
    long_form_weight: float
    threshold: int
    threshold_probability: float
    xpts_if_start: float


def project_benchwarmers_saves(
    *,
    saves_per_90: float,
    opponent_xg_per_match: float,
    league_average_xg_per_match: float,
    position: str,
) -> SavesRateProjection:
    """Apply the workbook fixture multiplier and expose exact save bundles."""
    _validate_non_negative(saves_per_90, "saves_per_90")
    _validate_non_negative(opponent_xg_per_match, "opponent_xg_per_match")
    if not isfinite(league_average_xg_per_match) or league_average_xg_per_match <= 0.0:
        raise ValueError("league_average_xg_per_match must be finite and positive")
    _position_value(DEFCON_POINTS_BY_POSITION, position)

    save_lambda = (
        saves_per_90 * opponent_xg_per_match / league_average_xg_per_match
        if position == "GK"
        else 0.0
    )
    return SavesRateProjection(
        save_lambda_if_start=float(save_lambda),
        workbook_xpts_if_start=float(save_lambda / SAVE_BUNDLE_SIZE),
        exact_xpts_if_start=float(
            expected_poisson_bundles(save_lambda, SAVE_BUNDLE_SIZE)
        ),
    )


def weight_saves(
    projection: SavesRateProjection,
    appearance: AppearanceProjection,
    *,
    substitute_to_start_minutes_ratio: float,
) -> WeightedExpectedPoints:
    """Apply start/substitute probabilities to discrete save bundles."""
    _validate_non_negative(
        substitute_to_start_minutes_ratio,
        "substitute_to_start_minutes_ratio",
    )
    start_xpts = appearance.start_probability * expected_poisson_bundles(
        projection.save_lambda_if_start,
        SAVE_BUNDLE_SIZE,
    )
    substitute_xpts = (
        appearance.substitute_appearance_probability
        * expected_poisson_bundles(
            projection.save_lambda_if_start * substitute_to_start_minutes_ratio,
            SAVE_BUNDLE_SIZE,
        )
    )
    return WeightedExpectedPoints(
        start_xpts=float(start_xpts),
        substitute_xpts=float(substitute_xpts),
        total_xpts=float(start_xpts + substitute_xpts),
    )


def project_discipline(
    *,
    yellow_card_rate_if_start: float,
    red_card_rate_if_start: float,
) -> DisciplineRateProjection:
    """Score card rates while retaining the workbook's disabled-red diagnostic."""
    _validate_non_negative(yellow_card_rate_if_start, "yellow_card_rate_if_start")
    _validate_non_negative(red_card_rate_if_start, "red_card_rate_if_start")
    yellow_xpts = yellow_card_rate_if_start * YELLOW_CARD_POINTS
    red_xpts = red_card_rate_if_start * RED_CARD_POINTS
    return DisciplineRateProjection(
        yellow_card_rate_if_start=float(yellow_card_rate_if_start),
        red_card_rate_if_start=float(red_card_rate_if_start),
        yellow_card_xpts_if_start=float(yellow_xpts),
        red_card_xpts_if_start=float(red_xpts),
        workbook_red_card_xpts_if_start=0.0,
        total_xpts_if_start=float(yellow_xpts + red_xpts),
    )


def weight_linear_component(
    xpts_if_start: float,
    appearance: AppearanceProjection,
    *,
    substitute_to_start_minutes_ratio: float,
) -> WeightedExpectedPoints:
    """Weight a minutes-linear component without assigning value to absences."""
    if not isfinite(xpts_if_start):
        raise ValueError("xpts_if_start must be finite")
    _validate_non_negative(
        substitute_to_start_minutes_ratio,
        "substitute_to_start_minutes_ratio",
    )
    start_xpts = appearance.start_probability * xpts_if_start
    substitute_xpts = (
        appearance.substitute_appearance_probability
        * substitute_to_start_minutes_ratio
        * xpts_if_start
    )
    return WeightedExpectedPoints(
        start_xpts=float(start_xpts),
        substitute_xpts=float(substitute_xpts),
        total_xpts=float(start_xpts + substitute_xpts),
    )


def _bonus_signal(
    *,
    starts: int,
    bonus_per_start: float,
    bps_per_start: float,
    league_average_bonus_per_bps: float,
    minimum_starts: int,
    bonus_weight: float,
) -> float:
    if starts > minimum_starts:
        return (
            bonus_weight * bonus_per_start
            + (1.0 - bonus_weight)
            * bps_per_start
            * league_average_bonus_per_bps
        )
    return bps_per_start * league_average_bonus_per_bps


def project_benchwarmers_bonus(
    *,
    previous_starts: int,
    previous_bonus_per_start: float,
    previous_bps_per_start: float,
    current_starts: int,
    current_bonus_per_start: float,
    current_bps_per_start: float,
    league_average_bonus_per_bps: float,
    defensive_fixture_multiplier: float,
    attacking_fixture_multiplier: float,
    position: str,
    previous_season_weight: float = 1.0,
    bonus_weight: float = 1.0,
    minimum_starts: int = 5,
) -> BonusRateProjection:
    """Reproduce the workbook bonus signal and apply a valid 0-3 bound."""
    for name, starts in (("previous_starts", previous_starts), ("current_starts", current_starts)):
        if isinstance(starts, bool) or not isinstance(starts, int) or starts < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if isinstance(minimum_starts, bool) or not isinstance(minimum_starts, int) or minimum_starts < 0:
        raise ValueError("minimum_starts must be a non-negative integer")
    for name, value in (
        ("previous_bonus_per_start", previous_bonus_per_start),
        ("previous_bps_per_start", previous_bps_per_start),
        ("current_bonus_per_start", current_bonus_per_start),
        ("current_bps_per_start", current_bps_per_start),
        ("league_average_bonus_per_bps", league_average_bonus_per_bps),
        ("defensive_fixture_multiplier", defensive_fixture_multiplier),
        ("attacking_fixture_multiplier", attacking_fixture_multiplier),
    ):
        _validate_non_negative(value, name)
    _validate_probability(previous_season_weight, "previous_season_weight")
    _validate_probability(bonus_weight, "bonus_weight")
    _position_value(DEFCON_POINTS_BY_POSITION, position)

    previous_signal = _bonus_signal(
        starts=previous_starts,
        bonus_per_start=previous_bonus_per_start,
        bps_per_start=previous_bps_per_start,
        league_average_bonus_per_bps=league_average_bonus_per_bps,
        minimum_starts=minimum_starts,
        bonus_weight=bonus_weight,
    )
    current_signal = _bonus_signal(
        starts=current_starts,
        bonus_per_start=current_bonus_per_start,
        bps_per_start=current_bps_per_start,
        league_average_bonus_per_bps=league_average_bonus_per_bps,
        minimum_starts=minimum_starts,
        bonus_weight=bonus_weight,
    )
    historical_rate = (
        previous_season_weight * previous_signal
        + (1.0 - previous_season_weight) * current_signal
    )
    fixture_multiplier = (
        defensive_fixture_multiplier
        if position in {"GK", "DEF"}
        else attacking_fixture_multiplier
    )
    workbook_uncapped = historical_rate * fixture_multiplier
    bounded = min(BONUS_POINTS_MAXIMUM, max(0.0, workbook_uncapped))
    return BonusRateProjection(
        historical_bonus_rate_if_start=float(historical_rate),
        fixture_multiplier=float(fixture_multiplier),
        workbook_uncapped_xpts_if_start=float(workbook_uncapped),
        bounded_xpts_if_start=float(bounded),
    )


def project_benchwarmers_defcon(
    *,
    long_form_lambda_if_start: float,
    short_form_lambda_if_start: float,
    position: str,
    long_form_weight: float = 1.0,
) -> DefConRateProjection:
    """Apply the workbook's discrete DefCon threshold model."""
    _validate_non_negative(long_form_lambda_if_start, "long_form_lambda_if_start")
    _validate_non_negative(short_form_lambda_if_start, "short_form_lambda_if_start")
    _validate_probability(long_form_weight, "long_form_weight")
    points = float(_position_value(DEFCON_POINTS_BY_POSITION, position))
    threshold = int(_position_value(DEFCON_THRESHOLD_BY_POSITION, position))
    long_probability = poisson_tail_probability(
        long_form_lambda_if_start,
        threshold,
    )
    short_probability = poisson_tail_probability(
        short_form_lambda_if_start,
        threshold,
    )
    threshold_probability = (
        long_form_weight * long_probability
        + (1.0 - long_form_weight) * short_probability
    )
    return DefConRateProjection(
        long_form_lambda_if_start=float(long_form_lambda_if_start),
        short_form_lambda_if_start=float(short_form_lambda_if_start),
        long_form_weight=float(long_form_weight),
        threshold=threshold,
        threshold_probability=float(threshold_probability),
        xpts_if_start=float(points * threshold_probability),
    )


def weight_defcon(
    projection: DefConRateProjection,
    appearance: AppearanceProjection,
    *,
    substitute_to_start_minutes_ratio: float,
    position: str,
) -> WeightedExpectedPoints:
    """Apply threshold probabilities separately to start and substitute branches."""
    _validate_non_negative(
        substitute_to_start_minutes_ratio,
        "substitute_to_start_minutes_ratio",
    )
    points = float(_position_value(DEFCON_POINTS_BY_POSITION, position))
    threshold = int(_position_value(DEFCON_THRESHOLD_BY_POSITION, position))
    start_xpts = appearance.start_probability * projection.xpts_if_start
    substitute_threshold_probability = (
        projection.long_form_weight
        * poisson_tail_probability(
            projection.long_form_lambda_if_start
            * substitute_to_start_minutes_ratio,
            threshold,
        )
        + (1.0 - projection.long_form_weight)
        * poisson_tail_probability(
            projection.short_form_lambda_if_start
            * substitute_to_start_minutes_ratio,
            threshold,
        )
    )
    substitute_xpts = (
        appearance.substitute_appearance_probability
        * points
        * substitute_threshold_probability
    )
    return WeightedExpectedPoints(
        start_xpts=float(start_xpts),
        substitute_xpts=float(substitute_xpts),
        total_xpts=float(start_xpts + substitute_xpts),
    )
