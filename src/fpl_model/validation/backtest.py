"""Deadline-safe walk-forward backtest records, folds, and basic metrics."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from math import isfinite, sqrt


def _validate_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class BacktestObservation:
    """One historical player-fixture prediction and its realised outcome.

    ``feature_cutoff`` records the newest information used to make the
    prediction. ``outcome_available_at`` records when the actual points became
    eligible for use as training/calibration history in a later fold.
    """

    season: str
    gameweek: int
    deadline: datetime
    fixture_kickoff: datetime
    feature_cutoff: datetime
    outcome_available_at: datetime
    player_id: int | str
    fixture_id: int | str
    predicted_xpts: float
    actual_points: float

    def __post_init__(self) -> None:
        if not self.season.strip():
            raise ValueError("season must not be blank")
        if isinstance(self.gameweek, bool) or not isinstance(self.gameweek, int):
            raise ValueError("gameweek must be an integer")
        if not 1 <= self.gameweek <= 38:
            raise ValueError("gameweek must be between 1 and 38")
        for name in (
            "deadline",
            "fixture_kickoff",
            "feature_cutoff",
            "outcome_available_at",
        ):
            _validate_aware(getattr(self, name), name)
        if self.feature_cutoff > self.deadline:
            raise ValueError("feature_cutoff must not be after the deadline")
        if self.deadline > self.fixture_kickoff:
            raise ValueError("deadline must not be after fixture_kickoff")
        if self.outcome_available_at < self.fixture_kickoff:
            raise ValueError("outcome cannot be available before fixture_kickoff")
        for name in ("player_id", "fixture_id"):
            value = getattr(self, name)
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"{name} must not be blank")
            if not isinstance(value, (int, str)) or isinstance(value, bool):
                raise ValueError(f"{name} must be an integer or string")
        for name in ("predicted_xpts", "actual_points"):
            if not isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """Training history available before one evaluation deadline."""

    deadline: datetime
    training: tuple[BacktestObservation, ...]
    evaluation: tuple[BacktestObservation, ...]


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    """Small transparent metric set for one evaluation sample."""

    observations: int
    mean_predicted_xpts: float
    mean_actual_points: float
    mean_error: float
    mean_absolute_error: float
    root_mean_squared_error: float


def walk_forward_folds(
    observations: Iterable[BacktestObservation],
    *,
    minimum_training_observations: int = 0,
) -> tuple[WalkForwardFold, ...]:
    """Build deadline folds using only outcomes available before each deadline."""
    if (
        isinstance(minimum_training_observations, bool)
        or not isinstance(minimum_training_observations, int)
        or minimum_training_observations < 0
    ):
        raise ValueError("minimum_training_observations must be a non-negative integer")
    rows = tuple(observations)
    identities: set[tuple[str, datetime, int | str, int | str]] = set()
    for row in rows:
        identity = (row.season, row.deadline, row.fixture_id, row.player_id)
        if identity in identities:
            raise ValueError("duplicate player-fixture prediction at one deadline")
        identities.add(identity)

    def sort_key(row: BacktestObservation) -> tuple[datetime, str, int, str, str]:
        return (
            row.deadline,
            row.season,
            row.gameweek,
            str(row.fixture_id),
            str(row.player_id),
        )

    folds: list[WalkForwardFold] = []
    for deadline in sorted({row.deadline for row in rows}):
        evaluation = tuple(
            sorted(
                (row for row in rows if row.deadline == deadline),
                key=sort_key,
            )
        )
        training = tuple(
            sorted(
                (
                    row
                    for row in rows
                    if row.deadline < deadline
                    and row.outcome_available_at <= deadline
                ),
                key=sort_key,
            )
        )
        if len(training) < minimum_training_observations:
            continue
        folds.append(
            WalkForwardFold(
                deadline=deadline,
                training=training,
                evaluation=evaluation,
            )
        )
    return tuple(folds)


def score_predictions(
    observations: Iterable[BacktestObservation],
) -> BacktestMetrics:
    """Calculate unweighted player-fixture prediction metrics."""
    rows = tuple(observations)
    if not rows:
        raise ValueError("at least one observation is required")
    errors = tuple(row.predicted_xpts - row.actual_points for row in rows)
    count = len(rows)
    return BacktestMetrics(
        observations=count,
        mean_predicted_xpts=float(sum(row.predicted_xpts for row in rows) / count),
        mean_actual_points=float(sum(row.actual_points for row in rows) / count),
        mean_error=float(sum(errors) / count),
        mean_absolute_error=float(sum(abs(error) for error in errors) / count),
        root_mean_squared_error=float(
            sqrt(sum(error * error for error in errors) / count)
        ),
    )
