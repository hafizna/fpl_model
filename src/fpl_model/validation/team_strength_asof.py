"""Vaastav-only, deadline-safe team strength for the walk-forward backtest.

This is a distinct, separately versioned methodology from the workbook-derived
``team_strength_projection``/``team_strength_run`` tables used by the GW1
preseason baseline. ``docs/PIPELINE_ARCHITECTURE.md`` already documents that
naively aggregating Vaastav's player-level xG does not reproduce the reviewed
workbook team rates; this module is that same kind of aggregation, used
deliberately here because the workbook has no historical time series to backtest
against. It must never be written into the workbook's DuckDB tables or presented
as reproducing them.

``gameweek < as_of_gameweek`` alone is not sufficient: a postponed fixture can
carry an earlier gameweek label while its kickoff -- and therefore its
outcome -- was only knowable after a later gameweek's deadline. This module
also requires ``kickoff_time + outcome_delay <= target_deadline`` when a
deadline is supplied, mirroring ``validation/backtest.py``'s own
``outcome_available_at <= deadline`` fold rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from fpl_model.model.defence import DefensiveWindow, corrected_team_xgc_per_match
from fpl_model.model.fixture import FixtureStrength

POLICY_VERSION = "vaastav_expanding_team_strength_v1"


@dataclass(frozen=True, slots=True)
class TeamStrengthAsOf:
    """One team's Vaastav-derived ``FixtureStrength`` as of one GW cutoff."""

    team: str
    as_of_gameweek: int
    matches_played: int
    short_form_matches_played: int
    strength: FixtureStrength
    data_quality_flags: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.team.strip():
            raise ValueError("team must not be blank")
        if not 1 <= self.as_of_gameweek <= 38:
            raise ValueError("as_of_gameweek must be between 1 and 38")
        if self.matches_played < 0 or self.short_form_matches_played < 0:
            raise ValueError("matches_played counts must be non-negative")
        if self.short_form_matches_played > self.matches_played:
            raise ValueError("short_form_matches_played cannot exceed matches_played")


def team_strength_as_of(
    team_fixture_results: pd.DataFrame,
    *,
    as_of_gameweek: int,
    short_form_gameweeks: int = 6,
    long_form_weight: float = 0.8,
    target_deadline: datetime | None = None,
    outcome_delay: timedelta = timedelta(hours=3),
) -> dict[str, TeamStrengthAsOf]:
    """Derive every team's ``FixtureStrength`` using only causally-available fixtures.

    A row must satisfy ``gameweek < as_of_gameweek``; when ``target_deadline``
    is given, it must *also* satisfy
    ``kickoff_time + outcome_delay <= target_deadline`` -- a postponed fixture
    can carry an earlier gameweek label while kicking off, and having its
    outcome known, only after a later gameweek's deadline. Mirrors
    ``materialize_preseason_team_strength``'s long/short xG and xGC blend and
    calls ``corrected_team_xgc_per_match`` (``defence.py``) directly rather
    than reimplementing its calibration. League averages are the unweighted
    mean of all teams' own blended rates at the same causal cutoff -- there is
    no workbook league-average constant to borrow in-season, so this is also
    correctly re-derived at every ``N``. A team with zero matches played
    before ``as_of_gameweek`` is omitted from the result (never assigned a
    fabricated league-average fallback); callers must treat a missing team as
    a gap.
    """
    if not 1 <= as_of_gameweek <= 38:
        raise ValueError("as_of_gameweek must be between 1 and 38")
    if not 1 <= short_form_gameweeks <= 38:
        raise ValueError("short_form_gameweeks must be between 1 and 38")
    if not 0.0 <= long_form_weight <= 1.0:
        raise ValueError("long_form_weight must be between 0 and 1")
    if target_deadline is not None and (
        target_deadline.tzinfo is None or target_deadline.utcoffset() is None
    ):
        raise ValueError("target_deadline must be timezone-aware")

    long_history = team_fixture_results.loc[
        team_fixture_results["gameweek"] < as_of_gameweek
    ]
    if target_deadline is not None:
        outcome_available_at = long_history["kickoff_time"] + outcome_delay
        long_history = long_history.loc[outcome_available_at <= target_deadline]
    if long_history.empty:
        return {}
    short_start = as_of_gameweek - short_form_gameweeks
    short_history = long_history.loc[long_history["gameweek"] >= short_start]

    long_by_team = long_history.groupby("team", sort=True)
    short_by_team = short_history.groupby("team", sort=True)

    raw_rates: dict[str, tuple[float, float, int, int]] = {}
    for team, group in long_by_team:
        short_group = (
            short_by_team.get_group(team)
            if team in short_by_team.groups
            else group.iloc[0:0]
        )
        long_matches = len(group)
        short_matches = len(short_group)
        long_xg_rate = group["team_xg_for"].sum() / long_matches
        short_xg_rate = (
            short_group["team_xg_for"].sum() / short_matches if short_matches else 0.0
        )
        blended_xg = (
            long_form_weight * long_xg_rate + (1.0 - long_form_weight) * short_xg_rate
        )
        corrected_xgc = corrected_team_xgc_per_match(
            DefensiveWindow(float(group["team_xg_against"].sum()), float(long_matches)),
            DefensiveWindow(
                float(short_group["team_xg_against"].sum()), float(short_matches)
            ),
            long_form_weight=long_form_weight,
        )
        raw_rates[str(team)] = (blended_xg, corrected_xgc, long_matches, short_matches)

    if not raw_rates:
        return {}
    league_average_xg = sum(value[0] for value in raw_rates.values()) / len(raw_rates)
    league_average_xgc = sum(value[1] for value in raw_rates.values()) / len(raw_rates)

    result: dict[str, TeamStrengthAsOf] = {}
    for team, (blended_xg, corrected_xgc, long_matches, short_matches) in raw_rates.items():
        flags: list[str] = []
        if short_matches < short_form_gameweeks:
            flags.append("SHORT_FORM_WINDOW_TRUNCATED")
        result[team] = TeamStrengthAsOf(
            team=team,
            as_of_gameweek=as_of_gameweek,
            matches_played=long_matches,
            short_form_matches_played=short_matches,
            strength=FixtureStrength(
                opponent_xg_per_match=blended_xg,
                opponent_xgc_per_match=corrected_xgc,
                league_average_xg_per_match=league_average_xg,
                league_average_xgc_per_match=league_average_xgc,
            ),
            data_quality_flags=tuple(flags),
        )
    return result
