"""Materialise deadline-safe observations from historical player-fixture facts."""

from __future__ import annotations

from collections.abc import Hashable
from datetime import timedelta
from math import isfinite
from numbers import Integral

import pandas as pd

from fpl_model.validation.backtest import BacktestObservation

REQUIRED_GAMEWEEK_COLUMNS = {
    "GW",
    "element",
    "fixture",
    "kickoff_time",
    "total_points",
}


def _normalise_identifier(value: object, name: str) -> int | str:
    if pd.isna(value):
        raise ValueError(f"{name} must not be missing")
    if isinstance(value, Integral) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and value.strip():
        return value
    raise ValueError(f"{name} must be an integer or non-blank string")


def _validate_duration(value: timedelta, name: str) -> None:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError(f"{name} must be a positive timedelta")


def infer_gameweek_deadlines(
    gameweeks: pd.DataFrame,
    *,
    deadline_buffer: timedelta = timedelta(minutes=90),
) -> dict[int, pd.Timestamp]:
    """Infer one conservative deadline before each GW's earliest kickoff.

    Vaastav's merged gameweek data does not carry official deadline timestamps.
    The inferred value is explicit metadata for smoke testing and must not be
    confused with an archived official deadline snapshot.
    """
    _validate_duration(deadline_buffer, "deadline_buffer")
    missing = {"GW", "kickoff_time"} - set(gameweeks.columns)
    if missing:
        raise ValueError(f"missing required columns: {', '.join(sorted(missing))}")
    kickoffs = pd.to_datetime(gameweeks["kickoff_time"], utc=True, errors="coerce")
    if kickoffs.isna().any():
        raise ValueError("kickoff_time contains missing or invalid values")
    numeric_gameweeks = pd.to_numeric(gameweeks["GW"], errors="coerce")
    if numeric_gameweeks.isna().any():
        raise ValueError("GW contains missing or invalid values")

    frame = pd.DataFrame({"GW": numeric_gameweeks.astype(int), "kickoff": kickoffs})
    earliest = frame.groupby("GW", sort=True)["kickoff"].min()
    deadlines: dict[int, pd.Timestamp] = {}
    for gameweek, kickoff in earliest.items():
        if not 1 <= int(gameweek) <= 38:
            raise ValueError("GW must be between 1 and 38")
        deadlines[int(gameweek)] = kickoff - deadline_buffer
    return deadlines


def materialize_expanding_player_mean_baseline(
    gameweeks: pd.DataFrame,
    *,
    season: str,
    deadline_buffer: timedelta = timedelta(minutes=90),
    outcome_delay: timedelta = timedelta(hours=3),
    cold_start_xpts: float = 0.0,
) -> tuple[BacktestObservation, ...]:
    """Create a causal smoke baseline from each player's prior realised points.

    This is deliberately *not* the Benchwarmers model. It validates historical
    joins, deadlines, postponed-fixture handling, folds, and metrics while the
    model's own deadline snapshots are not yet available.
    """
    if not season.strip():
        raise ValueError("season must not be blank")
    _validate_duration(outcome_delay, "outcome_delay")
    if not isfinite(cold_start_xpts):
        raise ValueError("cold_start_xpts must be finite")
    missing = REQUIRED_GAMEWEEK_COLUMNS - set(gameweeks.columns)
    if missing:
        raise ValueError(f"missing required columns: {', '.join(sorted(missing))}")

    frame = gameweeks.drop_duplicates().copy()
    identity_columns = ["GW", "fixture", "element"]
    if frame.duplicated(identity_columns, keep=False).any():
        raise ValueError("conflicting duplicate player-fixture rows")
    frame["_kickoff"] = pd.to_datetime(
        frame["kickoff_time"],
        utc=True,
        errors="coerce",
    )
    if frame["_kickoff"].isna().any():
        raise ValueError("kickoff_time contains missing or invalid values")
    frame["_outcome_available"] = frame["_kickoff"] + outcome_delay
    frame["_gameweek"] = pd.to_numeric(frame["GW"], errors="coerce")
    frame["_actual_points"] = pd.to_numeric(
        frame["total_points"],
        errors="coerce",
    )
    if frame[["_gameweek", "_actual_points"]].isna().any().any():
        raise ValueError("GW and total_points must be numeric and non-missing")
    frame["_gameweek"] = frame["_gameweek"].astype(int)
    deadlines = infer_gameweek_deadlines(
        frame,
        deadline_buffer=deadline_buffer,
    )

    observations: list[BacktestObservation] = []
    for gameweek, deadline in deadlines.items():
        history = frame.loc[frame["_outcome_available"] <= deadline]
        player_means: dict[Hashable, float] = (
            history.groupby("element", dropna=False)["_actual_points"].mean().to_dict()
        )
        evaluation = frame.loc[frame["_gameweek"] == gameweek]
        for row_data in evaluation.to_dict(orient="records"):
            player_id = _normalise_identifier(row_data["element"], "element")
            fixture_id = _normalise_identifier(row_data["fixture"], "fixture")
            predicted_xpts = float(player_means.get(row_data["element"], cold_start_xpts))
            observations.append(
                BacktestObservation(
                    season=season,
                    gameweek=gameweek,
                    deadline=deadline.to_pydatetime(),
                    fixture_kickoff=row_data["_kickoff"].to_pydatetime(),
                    feature_cutoff=deadline.to_pydatetime(),
                    outcome_available_at=row_data["_outcome_available"].to_pydatetime(),
                    player_id=player_id,
                    fixture_id=fixture_id,
                    predicted_xpts=predicted_xpts,
                    actual_points=float(row_data["_actual_points"]),
                )
            )
    observations.sort(
        key=lambda row: (
            row.deadline,
            str(row.fixture_id),
            str(row.player_id),
        )
    )
    return tuple(observations)
