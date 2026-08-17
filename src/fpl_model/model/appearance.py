"""Appearance and expected-minutes components for the baseline xPts model.

The model starts from a small distribution of mutually exclusive playing-time
scenarios. Keeping the distribution explicit avoids treating one expected-
minutes number as if it also identified start, appearance, and 60-minute
probabilities.

It also contains a small, explicit translation of the appearance priors found
in the Benchwarmers workbook. The translation keeps the useful historical
heuristics while reconciling them into a valid probability distribution. It
does not apply contextual multipliers.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import isclose, isfinite

REGULATION_MINUTES = 90.0
SIXTY_MINUTE_THRESHOLD = 60.0
BENCHWARMERS_APPEARANCE_CAP = 0.99
BENCHWARMERS_ZERO_HISTORY_FLOOR = 0.01
BENCHWARMERS_PREVIOUS_SIXTY_CAP = 0.98
BENCHWARMERS_CURRENT_SIXTY_CAP = 0.99
DEFAULT_START_MINUTES = 59.4
DEFAULT_SUBSTITUTE_MINUTES = 18.0


def _validate_probability(value: float, name: str) -> None:
    if not isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be finite and between 0 and 1")


@dataclass(frozen=True, slots=True)
class MinutesScenario:
    """One mutually exclusive player-fixture playing-time outcome."""

    probability: float
    minutes: float
    started: bool
    label: str | None = None

    def __post_init__(self) -> None:
        _validate_probability(self.probability, "probability")
        if not isfinite(self.minutes) or not 0.0 <= self.minutes <= REGULATION_MINUTES:
            raise ValueError("minutes must be finite and between 0 and 90")
        if self.started and self.minutes == 0.0:
            raise ValueError("a starting scenario must include positive minutes")


@dataclass(frozen=True, slots=True)
class SeasonAppearanceHistory:
    """Observed playing-time counts and means for one completed season window."""

    starts: int
    substitute_appearances: int
    unused_substitute: int
    minutes_per_start: float = 0.0
    minutes_per_substitute: float = 0.0

    def __post_init__(self) -> None:
        for name in ("starts", "substitute_appearances", "unused_substitute"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("minutes_per_start", "minutes_per_substitute"):
            value = getattr(self, name)
            if not isfinite(value) or not 0.0 <= value <= REGULATION_MINUTES:
                raise ValueError(f"{name} must be finite and between 0 and 90")

    @property
    def squad_selections(self) -> int:
        """Starts, substitute appearances, and unused-substitute selections."""
        return self.starts + self.substitute_appearances + self.unused_substitute


@dataclass(frozen=True, slots=True)
class AppearanceProjection:
    """Auditable appearance outputs for one player-fixture."""

    start_probability: float
    substitute_appearance_probability: float
    appearance_probability: float
    sixty_minute_probability: float
    expected_minutes: float
    appearance_xpts: float
    sixty_minute_xpts: float
    total_xpts: float


@dataclass(frozen=True, slots=True)
class ConditionalAppearanceScenario:
    """Reviewed playing-time assumptions conditional on being available."""

    start_probability_if_available: float
    substitute_probability_if_available: float
    sixty_probability_given_start: float
    minutes_per_start: float
    minutes_per_substitute: float

    def __post_init__(self) -> None:
        for name in (
            "start_probability_if_available",
            "substitute_probability_if_available",
            "sixty_probability_given_start",
        ):
            _validate_probability(getattr(self, name), name)
        if (
            self.start_probability_if_available
            + self.substitute_probability_if_available
            > 1.0
        ):
            raise ValueError("conditional start and substitute probabilities cannot exceed 1")
        for name in ("minutes_per_start", "minutes_per_substitute"):
            value = getattr(self, name)
            if not isfinite(value) or not 0.0 <= value <= REGULATION_MINUTES:
                raise ValueError(f"{name} must be finite and between 0 and 90")
        if self.start_probability_if_available > 0.0 and self.minutes_per_start == 0.0:
            raise ValueError("a positive start probability requires positive start minutes")
        if (
            self.substitute_probability_if_available > 0.0
            and self.minutes_per_substitute == 0.0
        ):
            raise ValueError(
                "a positive substitute probability requires positive substitute minutes"
            )
        if (
            self.start_probability_if_available == 0.0
            and self.sixty_probability_given_start > 0.0
        ):
            raise ValueError("60-minute probability requires a positive start probability")


def project_conditional_appearance(
    scenario: ConditionalAppearanceScenario,
    *,
    availability_probability: float,
) -> AppearanceProjection:
    """Convert reviewed conditional assumptions into unconditional outcomes."""
    _validate_probability(availability_probability, "availability_probability")
    start_probability = (
        availability_probability * scenario.start_probability_if_available
    )
    substitute_probability = (
        availability_probability * scenario.substitute_probability_if_available
    )
    appearance_probability = start_probability + substitute_probability
    sixty_probability = (
        start_probability * scenario.sixty_probability_given_start
    )
    expected_minutes = (
        start_probability * scenario.minutes_per_start
        + substitute_probability * scenario.minutes_per_substitute
    )
    return AppearanceProjection(
        start_probability=float(start_probability),
        substitute_appearance_probability=float(substitute_probability),
        appearance_probability=float(appearance_probability),
        sixty_minute_probability=float(sixty_probability),
        expected_minutes=float(expected_minutes),
        appearance_xpts=float(appearance_probability),
        sixty_minute_xpts=float(sixty_probability),
        total_xpts=float(appearance_probability + sixty_probability),
    )


def benchwarmers_appearance_probability(history: SeasonAppearanceHistory) -> float:
    """Reproduce MODEL ``PS %1``/``%1`` before seasonal blending.

    The workbook uses a one-percent floor when no starts or substitute
    appearances are recorded. Otherwise it discounts the complement of the
    unused-substitute rate by one percent.
    """
    if history.starts == 0 and history.substitute_appearances == 0:
        return BENCHWARMERS_ZERO_HISTORY_FLOOR

    return (
        1.0 - history.unused_substitute / history.squad_selections
    ) * BENCHWARMERS_APPEARANCE_CAP


def benchwarmers_start_probability(history: SeasonAppearanceHistory) -> float:
    """Reproduce the workbook's starts-per-squad-selection rate."""
    if history.squad_selections == 0:
        return 0.0
    return history.starts / history.squad_selections


def benchwarmers_sixty_minute_given_start_probability(
    history: SeasonAppearanceHistory,
    *,
    probability_cap: float = BENCHWARMERS_PREVIOUS_SIXTY_CAP,
) -> float:
    """Translate the workbook's minutes-per-start curve into ``P(60+ | start)``.

    The piecewise-linear penalty reproduces ``CONTROL!B13:C17``. The cap is a
    named argument because the extracted workbook uses 0.98 for previous-season
    history and 0.99 for current-season history.
    """
    if not isfinite(probability_cap) or not 0.5 <= probability_cap <= 1.0:
        raise ValueError("probability_cap must be finite and between 0.5 and 1")

    minutes = history.minutes_per_start
    breakpoints = (
        (50.0, 0.50),
        (60.0, 0.50),
        (70.0, 0.15),
        (80.0, 0.05),
        (90.0, 0.00),
    )
    if minutes < breakpoints[0][0]:
        penalty = breakpoints[0][1]
    elif minutes >= breakpoints[-1][0]:
        penalty = breakpoints[-1][1]
    else:
        penalty = breakpoints[-1][1]
        for (lower_minutes, lower_penalty), (
            upper_minutes,
            upper_penalty,
        ) in zip(breakpoints, breakpoints[1:], strict=True):
            if lower_minutes <= minutes <= upper_minutes:
                fraction = (minutes - lower_minutes) / (upper_minutes - lower_minutes)
                penalty = lower_penalty + fraction * (upper_penalty - lower_penalty)
                break

    return probability_cap - penalty


def project_benchwarmers_appearance(
    previous_history: SeasonAppearanceHistory,
    current_history: SeasonAppearanceHistory,
    *,
    availability_probability: float = 1.0,
    manual_start_probability: float | None = None,
    appearance_previous_weight: float = 1.0,
    sixty_previous_weight: float = 1.0,
) -> AppearanceProjection:
    """Build a coherent appearance projection from the workbook priors.

    ``MODEL!1`` is treated as total appearance probability and ``MODEL!2`` as
    conditional ``P(60+ | start)``. The workbook's raw start rate can exceed
    its capped appearance probability (for example, 1.00 versus 0.99 for a
    certain starter), so the start probability is capped at the appearance
    probability before the mutually exclusive start/substitute split is made.

    A manual start input replaces the historical start rate. It does not invoke
    the workbook's separate ``T1`` point-boost formula.
    """
    _validate_probability(availability_probability, "availability_probability")
    _validate_probability(appearance_previous_weight, "appearance_previous_weight")
    _validate_probability(sixty_previous_weight, "sixty_previous_weight")
    if manual_start_probability is not None:
        _validate_probability(manual_start_probability, "manual_start_probability")

    appearance_current_weight = 1.0 - appearance_previous_weight
    sixty_current_weight = 1.0 - sixty_previous_weight

    appearance_probability = availability_probability * (
        appearance_previous_weight
        * benchwarmers_appearance_probability(previous_history)
        + appearance_current_weight
        * benchwarmers_appearance_probability(current_history)
    )
    historical_start_probability = availability_probability * (
        appearance_previous_weight * benchwarmers_start_probability(previous_history)
        + appearance_current_weight * benchwarmers_start_probability(current_history)
    )
    requested_start_probability = (
        historical_start_probability
        if manual_start_probability is None
        else manual_start_probability
    )
    start_probability = min(requested_start_probability, appearance_probability)
    substitute_appearance_probability = appearance_probability - start_probability

    sixty_minute_given_start_probability = (
        sixty_previous_weight
        * benchwarmers_sixty_minute_given_start_probability(
            previous_history,
            probability_cap=BENCHWARMERS_PREVIOUS_SIXTY_CAP,
        )
        + sixty_current_weight
        * benchwarmers_sixty_minute_given_start_probability(
            current_history,
            probability_cap=BENCHWARMERS_CURRENT_SIXTY_CAP,
        )
    )
    sixty_minute_probability = (
        start_probability * sixty_minute_given_start_probability
    )

    previous_start_minutes = (
        previous_history.minutes_per_start or DEFAULT_START_MINUTES
    )
    current_start_minutes = current_history.minutes_per_start or DEFAULT_START_MINUTES
    previous_substitute_minutes = (
        previous_history.minutes_per_substitute or DEFAULT_SUBSTITUTE_MINUTES
    )
    current_substitute_minutes = (
        current_history.minutes_per_substitute or DEFAULT_SUBSTITUTE_MINUTES
    )
    blended_start_minutes = (
        appearance_previous_weight * previous_start_minutes
        + appearance_current_weight * current_start_minutes
    )
    blended_substitute_minutes = (
        appearance_previous_weight * previous_substitute_minutes
        + appearance_current_weight * current_substitute_minutes
    )
    expected_minutes = (
        start_probability * blended_start_minutes
        + substitute_appearance_probability * blended_substitute_minutes
    )

    appearance_xpts = appearance_probability
    sixty_minute_xpts = sixty_minute_probability
    return AppearanceProjection(
        start_probability=float(start_probability),
        substitute_appearance_probability=float(substitute_appearance_probability),
        appearance_probability=float(appearance_probability),
        sixty_minute_probability=float(sixty_minute_probability),
        expected_minutes=float(expected_minutes),
        appearance_xpts=float(appearance_xpts),
        sixty_minute_xpts=float(sixty_minute_xpts),
        total_xpts=float(appearance_xpts + sixty_minute_xpts),
    )


def project_appearance(
    scenarios: Iterable[MinutesScenario],
    *,
    probability_tolerance: float = 1e-9,
) -> AppearanceProjection:
    """Aggregate a complete playing-time distribution into xPts components.

    Scenario probabilities must sum to one, including a zero-minute absence
    scenario where applicable. Minutes exclude stoppage time, matching the FPL
    60-minute appearance threshold.
    """
    outcomes = tuple(scenarios)
    if not outcomes:
        raise ValueError("at least one minutes scenario is required")
    if probability_tolerance < 0 or not isfinite(probability_tolerance):
        raise ValueError("probability_tolerance must be finite and non-negative")

    total_probability = sum(outcome.probability for outcome in outcomes)
    if not isclose(total_probability, 1.0, rel_tol=0.0, abs_tol=probability_tolerance):
        raise ValueError("scenario probabilities must sum to 1")

    start_probability = sum(
        outcome.probability for outcome in outcomes if outcome.started
    )
    substitute_appearance_probability = sum(
        outcome.probability
        for outcome in outcomes
        if not outcome.started and outcome.minutes > 0.0
    )
    appearance_probability = start_probability + substitute_appearance_probability
    sixty_minute_probability = sum(
        outcome.probability
        for outcome in outcomes
        if outcome.minutes >= SIXTY_MINUTE_THRESHOLD
    )
    expected_minutes = sum(
        outcome.probability * outcome.minutes for outcome in outcomes
    )

    # Every appearance earns one point; reaching 60 minutes earns one more.
    appearance_xpts = appearance_probability
    sixty_minute_xpts = sixty_minute_probability

    return AppearanceProjection(
        start_probability=float(start_probability),
        substitute_appearance_probability=float(substitute_appearance_probability),
        appearance_probability=float(appearance_probability),
        sixty_minute_probability=float(sixty_minute_probability),
        expected_minutes=float(expected_minutes),
        appearance_xpts=float(appearance_xpts),
        sixty_minute_xpts=float(sixty_minute_xpts),
        total_xpts=float(appearance_xpts + sixty_minute_xpts),
    )
