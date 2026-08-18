"""Deadline-safe, per-gameweek player rate windows for the walk-forward backtest.

Adapts ``materialize_preseason_rate_history``'s fixed, season-end-anchored
window query into a causal ``gameweek < as_of_gameweek`` window, computed
directly against the already-imported ``player_fixture_history`` table. Nothing
here is persisted: ``player_rate_history_run``/``player_rate_history`` are
one-shot-per-run schemas, and repurposing them for one row per evaluation
gameweek would be schema abuse. This module runs the window query once per
gameweek inside the backtest driver instead.

``gameweek < as_of_gameweek`` alone is not sufficient: a fixture can be
labelled with an earlier gameweek number yet be postponed to kick off after a
later gameweek's deadline (or, symmetrically, its outcome may not have been
knowable by that deadline even if the kickoff itself was earlier). Every
window function here therefore also requires
``kickoff_time + outcome_delay <= target_deadline`` when a deadline is
supplied, mirroring ``validation/backtest.py``'s own
``outcome_available_at <= deadline`` fold rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import duckdb

ZERO_LONG_FORM_MINUTES = "ZERO_LONG_FORM_MINUTES"
ZERO_DEFCON_HISTORY_MINUTES = "ZERO_DEFCON_HISTORY_MINUTES"
ZERO_PRIOR_STARTS = "ZERO_PRIOR_STARTS"


@dataclass(frozen=True, slots=True)
class PlayerRatesAsOf:
    """One player's rate-window scalar inputs as of one gameweek cutoff."""

    player_code: int
    position: str
    as_of_gameweek: int
    season_minutes: int
    season_starts: int
    season_saves: int
    season_yellow_cards: int
    season_red_cards: int
    season_bonus: int
    season_bps: int
    long_form_minutes: int
    long_form_expected_goals: float
    long_form_expected_assists: float
    short_form_minutes: int
    short_form_expected_goals: float
    short_form_expected_assists: float
    long_form_defcon_minutes: int
    long_form_defensive_contribution: int
    short_form_defcon_minutes: int
    short_form_defensive_contribution: int
    data_quality_flags: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 1 <= self.as_of_gameweek <= 38:
            raise ValueError("as_of_gameweek must be between 1 and 38")
        if self.position not in {"GK", "DEF", "MID", "FWD"}:
            raise ValueError("position must be one of: GK, DEF, MID, FWD")
        # BPS can be genuinely negative (a poor individual match can score
        # below the workbook's baseline), matching player_rate_history's own
        # schema, which has no non-negative CHECK constraint on bps columns.
        for name in (
            "season_minutes",
            "season_starts",
            "season_saves",
            "season_yellow_cards",
            "season_red_cards",
            "season_bonus",
            "long_form_minutes",
            "short_form_minutes",
            "long_form_defcon_minutes",
            "long_form_defensive_contribution",
            "short_form_defcon_minutes",
            "short_form_defensive_contribution",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")


def has_usable_rate_history(rate: PlayerRatesAsOf) -> bool:
    """Require observed minutes, not merely a zero-minute provider placeholder."""
    return rate.season_minutes > 0 and rate.long_form_minutes > 0


def player_rates_as_of(
    connection: duckdb.DuckDBPyConnection,
    *,
    import_run_id: str,
    as_of_gameweek: int,
    short_form_gameweeks: int = 6,
    defcon_short_form_gameweeks: int = 10,
    target_deadline: datetime | None = None,
    outcome_delay: timedelta = timedelta(hours=3),
) -> dict[int, PlayerRatesAsOf]:
    """Return every player's rate window using only causally-available fixtures.

    A row must satisfy ``gameweek < as_of_gameweek``; when ``target_deadline``
    is given, it must *also* satisfy
    ``kickoff_time + outcome_delay <= target_deadline`` -- a postponed fixture
    can carry an earlier gameweek label while kicking off, and having its
    outcome known, only after a later gameweek's deadline. Long form is an
    expanding window over every prior, available gameweek of the season; short
    form and the DefCon short form are trailing windows ending just before
    ``as_of_gameweek``. Flags mirror ``materialize_preseason_rate_history``'s
    zero-history flags exactly, so a zero-minute placeholder is never
    interpreted as zero ability by a caller that reuses the same
    ``has_usable_rate_history`` gate as the GW1 baseline.
    """
    if not 1 <= as_of_gameweek <= 38:
        raise ValueError("as_of_gameweek must be between 1 and 38")
    if not 1 <= short_form_gameweeks <= 38:
        raise ValueError("short_form_gameweeks must be between 1 and 38")
    if not 1 <= defcon_short_form_gameweeks <= 38:
        raise ValueError("defcon_short_form_gameweeks must be between 1 and 38")
    if target_deadline is not None and (
        target_deadline.tzinfo is None or target_deadline.utcoffset() is None
    ):
        raise ValueError("target_deadline must be timezone-aware")

    short_start = as_of_gameweek - short_form_gameweeks
    defcon_short_start = as_of_gameweek - defcon_short_form_gameweeks
    outcome_available_clause = (
        "AND kickoff_time + ?::INTERVAL <= ?" if target_deadline is not None else ""
    )
    outcome_available_parameters: list[object] = (
        [f"{outcome_delay.total_seconds()} seconds", target_deadline]
        if target_deadline is not None
        else []
    )

    rows = connection.execute(
        f"""
        SELECT player_code, any_value(position),
               sum(minutes), sum(starts), sum(saves), sum(yellow_cards),
               sum(red_cards), sum(bonus), sum(bps),
               sum(minutes) FILTER (WHERE gameweek < ?),
               sum(expected_goals) FILTER (WHERE gameweek < ?),
               sum(expected_assists) FILTER (WHERE gameweek < ?),
               sum(minutes) FILTER (WHERE gameweek < ? AND gameweek >= ?),
               sum(expected_goals) FILTER (WHERE gameweek < ? AND gameweek >= ?),
               sum(expected_assists) FILTER (WHERE gameweek < ? AND gameweek >= ?),
               sum(minutes) FILTER (WHERE gameweek < ?),
               sum(defensive_contribution) FILTER (WHERE gameweek < ?),
               sum(minutes) FILTER (WHERE gameweek < ? AND gameweek >= ?),
               sum(defensive_contribution) FILTER (WHERE gameweek < ? AND gameweek >= ?)
        FROM player_fixture_history
        WHERE import_run_id = ? AND gameweek < ? {outcome_available_clause}
        GROUP BY player_code
        ORDER BY player_code
        """,
        [
            as_of_gameweek,
            as_of_gameweek,
            as_of_gameweek,
            as_of_gameweek,
            short_start,
            as_of_gameweek,
            short_start,
            as_of_gameweek,
            short_start,
            as_of_gameweek,
            as_of_gameweek,
            as_of_gameweek,
            defcon_short_start,
            as_of_gameweek,
            defcon_short_start,
            import_run_id,
            as_of_gameweek,
            *outcome_available_parameters,
        ],
    ).fetchall()

    result: dict[int, PlayerRatesAsOf] = {}
    for row in rows:
        (
            player_code,
            position,
            season_minutes,
            season_starts,
            season_saves,
            season_yellow_cards,
            season_red_cards,
            season_bonus,
            season_bps,
            long_minutes,
            long_xg,
            long_xa,
            short_minutes,
            short_xg,
            short_xa,
            long_defcon_minutes,
            long_defcon,
            short_defcon_minutes,
            short_defcon,
        ) = row
        flags: set[str] = set()
        if long_minutes == 0:
            flags.add(ZERO_LONG_FORM_MINUTES)
        if long_defcon_minutes == 0:
            flags.add(ZERO_DEFCON_HISTORY_MINUTES)
        if season_starts == 0:
            flags.add(ZERO_PRIOR_STARTS)
        result[int(player_code)] = PlayerRatesAsOf(
            player_code=int(player_code),
            position=str(position),
            as_of_gameweek=as_of_gameweek,
            season_minutes=int(season_minutes),
            season_starts=int(season_starts),
            season_saves=int(season_saves),
            season_yellow_cards=int(season_yellow_cards),
            season_red_cards=int(season_red_cards),
            season_bonus=int(season_bonus),
            season_bps=int(season_bps),
            long_form_minutes=int(long_minutes),
            long_form_expected_goals=float(long_xg),
            long_form_expected_assists=float(long_xa),
            short_form_minutes=int(short_minutes),
            short_form_expected_goals=float(short_xg),
            short_form_expected_assists=float(short_xa),
            long_form_defcon_minutes=int(long_defcon_minutes),
            long_form_defensive_contribution=int(long_defcon),
            short_form_defcon_minutes=int(short_defcon_minutes),
            short_form_defensive_contribution=int(short_defcon),
            data_quality_flags=tuple(sorted(flags)),
        )
    return result


def league_average_bonus_rates_as_of(
    connection: duckdb.DuckDBPyConnection,
    *,
    import_run_id: str,
    as_of_gameweek: int,
    target_deadline: datetime | None = None,
    outcome_delay: timedelta = timedelta(hours=3),
) -> tuple[float, float, float]:
    """Return ``(avg_bps_per_start, avg_bonus_per_start, avg_bonus_per_bps)``.

    Uses only ``gameweek < as_of_gameweek`` and, when ``target_deadline`` is
    given, only fixtures whose outcome was available by that deadline
    (``kickoff_time + outcome_delay <= target_deadline``) -- see
    ``player_rates_as_of`` for why the gameweek number alone is insufficient.
    This replaces ``baseline_pipeline.py``'s hard-coded workbook previous-PL-
    season constants for this Vaastav-only, in-season backtest, so the
    methodology stays causal and does not silently blend in a non-Vaastav
    prior.
    """
    if not 1 <= as_of_gameweek <= 38:
        raise ValueError("as_of_gameweek must be between 1 and 38")
    if target_deadline is not None and (
        target_deadline.tzinfo is None or target_deadline.utcoffset() is None
    ):
        raise ValueError("target_deadline must be timezone-aware")

    outcome_available_clause = (
        "AND kickoff_time + ?::INTERVAL <= ?" if target_deadline is not None else ""
    )
    outcome_available_parameters: list[object] = (
        [f"{outcome_delay.total_seconds()} seconds", target_deadline]
        if target_deadline is not None
        else []
    )
    total_starts, total_bonus, total_bps = connection.execute(
        f"""
        SELECT sum(starts), sum(bonus), sum(bps)
        FROM player_fixture_history
        WHERE import_run_id = ? AND gameweek < ? {outcome_available_clause}
        """,
        [import_run_id, as_of_gameweek, *outcome_available_parameters],
    ).fetchone()
    total_starts = total_starts or 0
    if total_starts == 0:
        raise ValueError(
            f"no starts recorded before gameweek {as_of_gameweek}; "
            "league-average bonus rates are undefined"
        )
    avg_bonus_per_start = float(total_bonus) / total_starts
    avg_bps_per_start = float(total_bps) / total_starts
    avg_bonus_per_bps = float(total_bonus) / float(total_bps) if total_bps else 0.0
    return avg_bps_per_start, avg_bonus_per_start, avg_bonus_per_bps
