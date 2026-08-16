"""Clean-sheet and goals-conceded components for the baseline xPts model.

The team-rate and fixture stages follow the live Benchwarmers workbook path.
The final exposure stage applies the explicit 60-minute probability for clean
sheets and the repeated FPL deduction for every two goals conceded.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import exp, isfinite

from fpl_model.model.appearance import AppearanceProjection

CLEAN_SHEET_POINTS_BY_POSITION = {
    "GK": 4.0,
    "DEF": 4.0,
    "MID": 1.0,
    "FWD": 0.0,
}
GOALS_CONCEDED_PENALTY_BY_POSITION = {
    "GK": -1.0,
    "DEF": -1.0,
    "MID": 0.0,
    "FWD": 0.0,
}


def _validate_probability(value: float, name: str) -> None:
    if not isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be finite and between 0 and 1")


def _validate_non_negative(value: float, name: str) -> None:
    if not isfinite(value) or value < 0.0:
        raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class DefensiveWindow:
    """Team expected goals conceded and matches in one historical window."""

    expected_goals_conceded: float
    matches_played: float

    def __post_init__(self) -> None:
        _validate_non_negative(
            self.expected_goals_conceded,
            "expected_goals_conceded",
        )
        _validate_non_negative(self.matches_played, "matches_played")
        if self.matches_played == 0.0 and self.expected_goals_conceded > 0.0:
            raise ValueError("non-zero xGC requires positive matches_played")

    @property
    def xgc_per_match(self) -> float:
        """Return the workbook's team xGC/90 proxy, or zero without history."""
        if self.matches_played == 0.0:
            return 0.0
        return self.expected_goals_conceded / self.matches_played


@dataclass(frozen=True, slots=True)
class DefensiveRateProjection:
    """Fixture-level defensive probabilities before player appearance exposure."""

    corrected_team_xgc_per_match: float
    poisson_lambda: float
    clean_sheet_probability: float
    two_plus_goals_conceded_probability: float
    expected_goals_conceded_pairs: float
    clean_sheet_xpts_if_eligible: float
    workbook_goals_conceded_xpts_if_eligible: float
    exact_goals_conceded_xpts_if_full_match: float


@dataclass(frozen=True, slots=True)
class DefensiveProjection:
    """Unconditional clean-sheet and goals-conceded xPts for one player-fixture."""

    rate_projection: DefensiveRateProjection
    clean_sheet_xpts: float
    goals_conceded_xpts: float
    total_xpts: float


def corrected_team_xgc_per_match(
    long_form: DefensiveWindow,
    short_form: DefensiveWindow,
    *,
    long_form_weight: float = 0.8,
    calibration_raw_low: float = 0.9,
    calibration_multiplier_low: float = 0.9,
    calibration_raw_high: float = 2.0,
    calibration_multiplier_high: float = 1.0,
) -> float:
    """Blend team xGC windows and apply the workbook's linear correction."""
    _validate_probability(long_form_weight, "long_form_weight")
    for name, value in (
        ("calibration_raw_low", calibration_raw_low),
        ("calibration_multiplier_low", calibration_multiplier_low),
        ("calibration_raw_high", calibration_raw_high),
        ("calibration_multiplier_high", calibration_multiplier_high),
    ):
        _validate_non_negative(value, name)
    if calibration_raw_low == calibration_raw_high:
        raise ValueError("calibration raw points must be distinct")

    raw_rate = (
        long_form_weight * long_form.xgc_per_match
        + (1.0 - long_form_weight) * short_form.xgc_per_match
    )
    slope = (
        calibration_multiplier_high - calibration_multiplier_low
    ) / (calibration_raw_high - calibration_raw_low)
    intercept = calibration_multiplier_low - slope * calibration_raw_low
    correction_multiplier = intercept + slope * raw_rate
    if correction_multiplier < 0.0:
        raise ValueError("calibration produces a negative correction multiplier")
    return raw_rate * correction_multiplier


def expected_goals_conceded_pairs(poisson_lambda: float) -> float:
    """Return ``E[floor(goals_conceded / 2)]`` for a Poisson distribution."""
    _validate_non_negative(poisson_lambda, "poisson_lambda")
    probability_odd = (1.0 - exp(-2.0 * poisson_lambda)) / 2.0
    return (poisson_lambda - probability_odd) / 2.0


def project_benchwarmers_defensive_rates(
    *,
    corrected_team_xgc_per_match: float,
    opponent_xg_per_match: float,
    league_average_xg_per_match: float,
    position: str,
) -> DefensiveRateProjection:
    """Reproduce the live workbook Poisson CS/GC path and expose its GC approximation."""
    _validate_non_negative(
        corrected_team_xgc_per_match,
        "corrected_team_xgc_per_match",
    )
    _validate_non_negative(opponent_xg_per_match, "opponent_xg_per_match")
    if not isfinite(league_average_xg_per_match) or league_average_xg_per_match <= 0.0:
        raise ValueError("league_average_xg_per_match must be finite and positive")
    try:
        clean_sheet_points = CLEAN_SHEET_POINTS_BY_POSITION[position]
        goals_conceded_penalty = GOALS_CONCEDED_PENALTY_BY_POSITION[position]
    except KeyError as error:
        valid_positions = ", ".join(CLEAN_SHEET_POINTS_BY_POSITION)
        raise ValueError(f"position must be one of: {valid_positions}") from error

    poisson_lambda = (
        corrected_team_xgc_per_match
        * opponent_xg_per_match
        / league_average_xg_per_match
    )
    clean_sheet_probability = exp(-poisson_lambda)
    two_plus_goals_conceded_probability = 1.0 - clean_sheet_probability * (
        1.0 + poisson_lambda
    )
    expected_pairs = expected_goals_conceded_pairs(poisson_lambda)

    return DefensiveRateProjection(
        corrected_team_xgc_per_match=float(corrected_team_xgc_per_match),
        poisson_lambda=float(poisson_lambda),
        clean_sheet_probability=float(clean_sheet_probability),
        two_plus_goals_conceded_probability=float(
            two_plus_goals_conceded_probability
        ),
        expected_goals_conceded_pairs=float(expected_pairs),
        clean_sheet_xpts_if_eligible=float(
            clean_sheet_points * clean_sheet_probability
        ),
        workbook_goals_conceded_xpts_if_eligible=float(
            goals_conceded_penalty * two_plus_goals_conceded_probability
        ),
        exact_goals_conceded_xpts_if_full_match=float(
            goals_conceded_penalty * expected_pairs
        ),
    )


def weight_defensive_rates(
    rate_projection: DefensiveRateProjection,
    appearance: AppearanceProjection,
    *,
    substitute_to_start_minutes_ratio: float,
    position: str,
) -> DefensiveProjection:
    """Apply 60-minute CS eligibility and start/substitute GC exposure.

    Clean-sheet points use the unconditional 60-minute probability. Goals
    conceded use the exact repeated deduction under a full-match Poisson rate;
    the substitute branch rescales lambda before applying the nonlinear rule.
    """
    _validate_probability(
        substitute_to_start_minutes_ratio,
        "substitute_to_start_minutes_ratio",
    )
    try:
        goals_conceded_penalty = GOALS_CONCEDED_PENALTY_BY_POSITION[position]
    except KeyError as error:
        valid_positions = ", ".join(GOALS_CONCEDED_PENALTY_BY_POSITION)
        raise ValueError(f"position must be one of: {valid_positions}") from error

    clean_sheet_xpts = (
        rate_projection.clean_sheet_xpts_if_eligible
        * appearance.sixty_minute_probability
    )
    start_goals_conceded_xpts = (
        appearance.start_probability
        * goals_conceded_penalty
        * expected_goals_conceded_pairs(rate_projection.poisson_lambda)
    )
    substitute_goals_conceded_xpts = (
        appearance.substitute_appearance_probability
        * goals_conceded_penalty
        * expected_goals_conceded_pairs(
            rate_projection.poisson_lambda * substitute_to_start_minutes_ratio
        )
    )
    goals_conceded_xpts = (
        start_goals_conceded_xpts + substitute_goals_conceded_xpts
    )

    return DefensiveProjection(
        rate_projection=rate_projection,
        clean_sheet_xpts=float(clean_sheet_xpts),
        goals_conceded_xpts=float(goals_conceded_xpts),
        total_xpts=float(clean_sheet_xpts + goals_conceded_xpts),
    )
