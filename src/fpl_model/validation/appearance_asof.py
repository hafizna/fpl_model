"""Deadline-safe, empirical appearance projection for the walk-forward backtest.

Vaastav's zero-minute rows cannot distinguish an unused substitute from a
player outside the matchday squad (see ``docs/DATA_MODEL.md``'s appearance-
history import boundary), so this module does not attempt to reproduce
``project_benchwarmers_appearance``'s ``SeasonAppearanceHistory.unused_substitute``
logic. Instead it builds an empirical, probability-summing-to-1 distribution of
realised per-fixture ``(minutes, started)`` outcomes and feeds it to
``project_appearance``, which has no such dependency.

``gameweek < as_of_gameweek`` alone is not sufficient: a postponed fixture can
carry an earlier gameweek label while its kickoff -- and therefore its
outcome -- was only knowable after a later gameweek's deadline. This module
also requires ``kickoff_time + outcome_delay <= target_deadline`` when a
deadline is supplied, mirroring ``validation/backtest.py``'s own
``outcome_available_at <= deadline`` fold rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import duckdb

from fpl_model.model.appearance import (
    DEFAULT_START_MINUTES,
    DEFAULT_SUBSTITUTE_MINUTES,
    AppearanceProjection,
    MinutesScenario,
    project_appearance,
)

MISSING_APPEARANCE_HISTORY = "MISSING_APPEARANCE_HISTORY"


@dataclass(frozen=True, slots=True)
class AppearanceHistoryAsOf:
    """One player's empirical appearance projection as of one gameweek cutoff."""

    player_code: int
    as_of_gameweek: int
    fixtures_considered: int
    appearance: AppearanceProjection
    mean_minutes_per_start: float
    mean_minutes_per_substitute: float
    data_quality_flags: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 1 <= self.as_of_gameweek <= 38:
            raise ValueError("as_of_gameweek must be between 1 and 38")
        if self.fixtures_considered <= 0:
            raise ValueError("fixtures_considered must be positive")
        if self.mean_minutes_per_start <= 0.0 or self.mean_minutes_per_substitute <= 0.0:
            raise ValueError("mean minutes must be positive")


def build_minutes_scenarios(
    minutes_values: Sequence[float],
    started_values: Sequence[bool],
) -> tuple[MinutesScenario, ...]:
    """Turn realised ``(minutes, started)`` fixture rows into equal-probability scenarios.

    Each observed fixture becomes one scenario at probability ``1/N``; ties on
    identical ``(minutes, started)`` are not merged, keeping this a literal
    frequency count rather than a deduplicated distribution --
    ``project_appearance`` does not require merged/unique scenarios, and a
    plain frequency count is the simplest, most auditable empirical estimator.
    """
    if len(minutes_values) != len(started_values):
        raise ValueError("minutes_values and started_values must be the same length")
    if not minutes_values:
        raise ValueError("at least one fixture row is required")
    probability = 1.0 / len(minutes_values)
    return tuple(
        MinutesScenario(probability=probability, minutes=float(minutes), started=bool(started))
        for minutes, started in zip(minutes_values, started_values, strict=True)
    )


def appearance_as_of(
    connection: duckdb.DuckDBPyConnection,
    *,
    import_run_id: str,
    as_of_gameweek: int,
    window_gameweeks: int | None = None,
    target_deadline: datetime | None = None,
    outcome_delay: timedelta = timedelta(hours=3),
) -> dict[int, AppearanceHistoryAsOf]:
    """Build one empirical appearance projection per player as of one gameweek.

    Uses only fixture rows with ``gameweek < as_of_gameweek`` (expanding, or a
    trailing window of ``window_gameweeks`` gameweeks when given) and, when
    ``target_deadline`` is given, only rows whose outcome was available by
    that deadline (``kickoff_time + outcome_delay <= target_deadline``) -- a
    postponed fixture can carry an earlier gameweek label while its outcome
    was only knowable later. Players with zero fixture rows before the cutoff
    are omitted from the result -- never given a fabricated default-minutes
    scenario -- so callers must treat a missing player as a cold-start gap.
    """
    if not 1 <= as_of_gameweek <= 38:
        raise ValueError("as_of_gameweek must be between 1 and 38")
    if window_gameweeks is not None and not 1 <= window_gameweeks <= 38:
        raise ValueError("window_gameweeks must be between 1 and 38")
    if target_deadline is not None and (
        target_deadline.tzinfo is None or target_deadline.utcoffset() is None
    ):
        raise ValueError("target_deadline must be timezone-aware")

    parameters: list[object] = [import_run_id, as_of_gameweek]
    lower_bound_clause = ""
    if window_gameweeks is not None:
        lower_bound_clause = "AND gameweek >= ?"
        parameters.append(as_of_gameweek - window_gameweeks)
    outcome_available_clause = ""
    if target_deadline is not None:
        outcome_available_clause = "AND kickoff_time + ?::INTERVAL <= ?"
        parameters.append(f"{outcome_delay.total_seconds()} seconds")
        parameters.append(target_deadline)

    rows = connection.execute(
        f"""
        SELECT player_code, minutes, starts
        FROM player_fixture_history
        WHERE import_run_id = ? AND gameweek < ?
              {lower_bound_clause} {outcome_available_clause}
        ORDER BY player_code, gameweek, fixture_id
        """,
        parameters,
    ).fetchall()

    by_player: dict[int, tuple[list[float], list[bool]]] = {}
    for player_code, minutes, starts in rows:
        minutes_list, started_list = by_player.setdefault(int(player_code), ([], []))
        minutes_list.append(float(minutes))
        started_list.append(bool(starts))

    result: dict[int, AppearanceHistoryAsOf] = {}
    for player_code, (minutes_list, started_list) in by_player.items():
        scenarios = build_minutes_scenarios(minutes_list, started_list)
        start_minutes = [
            m for m, started in zip(minutes_list, started_list, strict=True) if started
        ]
        substitute_minutes = [
            m
            for m, started in zip(minutes_list, started_list, strict=True)
            if not started and m > 0.0
        ]
        mean_minutes_per_start = (
            sum(start_minutes) / len(start_minutes) if start_minutes else DEFAULT_START_MINUTES
        )
        mean_minutes_per_substitute = (
            sum(substitute_minutes) / len(substitute_minutes)
            if substitute_minutes
            else DEFAULT_SUBSTITUTE_MINUTES
        )
        result[player_code] = AppearanceHistoryAsOf(
            player_code=player_code,
            as_of_gameweek=as_of_gameweek,
            fixtures_considered=len(minutes_list),
            appearance=project_appearance(scenarios),
            mean_minutes_per_start=mean_minutes_per_start,
            mean_minutes_per_substitute=mean_minutes_per_substitute,
            data_quality_flags=(),
        )
    return result
