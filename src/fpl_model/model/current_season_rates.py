"""Materialize a deadline-safe current-season player-rate update.

P0 (`README.md`'s "Production critical path") asks for a current-season
player-rate update from final official xG, xA, DefCon, saves, cards, BPS, and
minutes, with small-sample shrinkage and no retrospective post-match xP
leakage. `baseline_pipeline.py`'s in-season path currently still consumes only
the frozen previous-season `player_rate_history` (flagged
`FROZEN_PREVIOUS_SEASON_PLAYER_RATES`) -- this module closes that gap.

Deadline safety: a row is built ONLY from `fpl_event_live_run` entries that
were already FINAL (`event_finished AND data_checked`) as of this run's own
``as_of`` -- mirroring `validation/material_conflict.py`'s own finality gate.
A provisional Gameweek, or a Gameweek not yet captured at all, is simply
excluded from the window rather than guessed. This also means the Gameweek
being scored is never included in its own rate window: by construction, a
completed Gameweek's own final outcome can only enter a LATER Gameweek's rate
update, never the projection that predicted it.

Shrinkage: each per-90 rate is blended toward the SAME player's own
previous-season rate (the latest `player_rate_history` row for that
`player_code`), weighted by `SHRINKAGE_PRIOR_MINUTES` --

    blended = (current_minutes * current_rate + K * prior_rate)
              / (current_minutes + K)

so a player with only a handful of current-season minutes reads close to
their own established rate, and a player with a full current-season sample
reads close to their current-season rate alone. `SHRINKAGE_PRIOR_MINUTES`
reuses `baseline_pipeline.py`'s own `PRIOR_REFERENCE_MINUTES` (900.0, ten full
matches) rather than inventing an independent constant. A player with no
previous-season rate history at all is NOT rescued here -- this module leaves
`prior_source = 'no_previous_season_history'` and a raw (unshrunk) current-
season rate; `baseline_pipeline.py`'s own existing empirical cohort-average
prior remains the fallback for that case (the Tzolis-shaped regression case
in `tests/test_baseline_pipeline_regression_cases.py`), so this module does
not duplicate or compete with that mechanism.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb

from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database

POLICY_VERSION = "current_season_rate_shrinkage_v1"
REGULATION_MINUTES = 90.0
# Reuses model/baseline_pipeline.py's own PRIOR_REFERENCE_MINUTES (10 full
# matches) as the shrinkage pseudo-count, so "how much evidence is needed
# before a rate is trusted" means the same number in both places.
SHRINKAGE_PRIOR_MINUTES = 900.0

NO_FINAL_GAMEWEEKS = "NO_FINAL_GAMEWEEKS"
NO_PREVIOUS_SEASON_RATE_HISTORY = "NO_PREVIOUS_SEASON_RATE_HISTORY"
ZERO_CURRENT_SEASON_MINUTES = "ZERO_CURRENT_SEASON_MINUTES"


@dataclass(frozen=True, slots=True)
class CurrentSeasonRateRunResult:
    rate_run_id: str
    source_ingestion_run_id: str
    season: str
    as_of_gameweek: int
    as_of: datetime
    final_gameweeks: tuple[int, ...]
    player_rows: int
    status: str


def _rate_per_90(total: float, minutes: float) -> float:
    return total / minutes * REGULATION_MINUTES if minutes > 0.0 else 0.0


def _blend(current_total: float, current_minutes: float, prior_per_90: float) -> float:
    """Bayesian shrinkage of one per-90 rate toward a prior, weighted by minutes."""
    current_per_90 = _rate_per_90(current_total, current_minutes)
    weight = current_minutes / (current_minutes + SHRINKAGE_PRIOR_MINUTES)
    return weight * current_per_90 + (1.0 - weight) * prior_per_90


def materialize_current_season_rates(
    *,
    source_ingestion_run_id: str,
    season: str,
    as_of_gameweek: int,
    as_of: datetime,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> CurrentSeasonRateRunResult:
    """Materialize one deadline-safe, shrunk current-season rate snapshot.

    Only Gameweeks strictly before ``as_of_gameweek`` are eligible, and even
    an eligible Gameweek is included only if its `fpl_event_live_run` was
    already FINAL (`event_finished AND data_checked`) at or before ``as_of``
    -- a Gameweek whose live data has not yet been checked by FPL is treated
    the same as one that has not been captured at all, never guessed at.
    """
    if not 1 <= as_of_gameweek <= 38:
        raise ValueError("as_of_gameweek must be between 1 and 38")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if not season.strip():
        raise ValueError("season must not be blank")

    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        final_runs = connection.execute(
            """
            SELECT live_run_id, gameweek
            FROM fpl_event_live_run
            WHERE source_ingestion_run_id = ?
              AND season = ?
              AND gameweek < ?
              AND event_finished AND data_checked
              AND captured_at <= ?
            QUALIFY row_number() OVER (
                PARTITION BY gameweek ORDER BY captured_at DESC
            ) = 1
            """,
            [source_ingestion_run_id, season, as_of_gameweek, as_of],
        ).fetchall()

        rate_run_id = (
            f"current_season_rates_gw{as_of_gameweek}_"
            f"{as_of.strftime('%Y%m%dT%H%M%SZ')}"
        )
        existing = connection.execute(
            "SELECT status, player_rows FROM current_season_player_rate_run WHERE rate_run_id = ?",
            [rate_run_id],
        ).fetchone()
        if existing is not None:
            # This run_id is already immutable and complete -- re-derive
            # which Gameweeks it covers by re-running the same eligibility
            # query rather than storing a redundant column, so a repeated
            # call is idempotent without a second source of truth for it.
            replay_gameweeks = tuple(
                sorted(
                    {
                        int(row[0])
                        for row in connection.execute(
                            """
                            SELECT gameweek FROM fpl_event_live_run
                            WHERE source_ingestion_run_id = ? AND season = ?
                              AND gameweek < ? AND event_finished AND data_checked
                              AND captured_at <= ?
                            """,
                            [source_ingestion_run_id, season, as_of_gameweek, as_of],
                        ).fetchall()
                    }
                )
            )
            return CurrentSeasonRateRunResult(
                rate_run_id=rate_run_id,
                source_ingestion_run_id=source_ingestion_run_id,
                season=season,
                as_of_gameweek=as_of_gameweek,
                as_of=as_of,
                final_gameweeks=replay_gameweeks,
                player_rows=int(existing[1]),
                status=str(existing[0]),
            )

        if not final_runs:
            connection.execute(
                """
                INSERT INTO current_season_player_rate_run VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 0, 'completed', current_timestamp
                )
                """,
                [
                    rate_run_id,
                    source_ingestion_run_id,
                    season,
                    as_of_gameweek,
                    as_of,
                    SHRINKAGE_PRIOR_MINUTES,
                    POLICY_VERSION,
                ],
            )
            return CurrentSeasonRateRunResult(
                rate_run_id=rate_run_id,
                source_ingestion_run_id=source_ingestion_run_id,
                season=season,
                as_of_gameweek=as_of_gameweek,
                as_of=as_of,
                final_gameweeks=(),
                player_rows=0,
                status="completed",
            )

        live_run_ids = [row[0] for row in final_runs]
        final_gameweeks = tuple(sorted({int(row[1]) for row in final_runs}))
        placeholders = ",".join("?" * len(live_run_ids))

        current_rows = connection.execute(
            f"""
            SELECT s.player_code, any_value(ps.fpl_id), any_value(ps.fpl_position),
                   sum(s.minutes), sum(s.starts), sum(s.expected_goals),
                   sum(s.expected_assists), sum(s.defensive_contribution),
                   sum(s.saves)
            FROM player_gameweek_stat AS s
            JOIN player_snapshot AS ps
              ON ps.player_code = s.player_code AND ps.ingestion_run_id = ?
            WHERE s.live_run_id IN ({placeholders}) AND s.player_code IS NOT NULL
            GROUP BY s.player_code
            """,
            [source_ingestion_run_id, *live_run_ids],
        ).fetchall()

        prior_rows = {
            int(row[0]): (float(row[1]), float(row[2]), float(row[3]))
            for row in connection.execute(
                """
                SELECT DISTINCT ON (player_code) player_code,
                       long_form_expected_goals, long_form_expected_assists,
                       long_form_minutes
                FROM player_rate_history
                WHERE rate_run_id = (
                    SELECT rate_run_id FROM player_rate_history_run
                    ORDER BY created_at DESC, rate_run_id DESC LIMIT 1
                )
                ORDER BY player_code
                """
            ).fetchall()
        }
        # DefCon/saves have no equivalent previous-season per-player rate in
        # player_rate_history's own long-form columns above and beyond the
        # long-form minutes already read there, so their own prior uses the
        # SAME long-form-minutes denominator against player_rate_history's
        # own long_form_defensive_contribution -- read separately below to
        # avoid overloading the 3-tuple above.
        secondary_prior_rows = {
            int(row[0]): (float(row[1]), float(row[2]))
            for row in connection.execute(
                """
                SELECT DISTINCT ON (player_code) player_code,
                       long_form_defensive_contribution, long_form_minutes
                FROM player_rate_history
                WHERE rate_run_id = (
                    SELECT rate_run_id FROM player_rate_history_run
                    ORDER BY created_at DESC, rate_run_id DESC LIMIT 1
                )
                ORDER BY player_code
                """
            ).fetchall()
        }
        # player_rate_history has no per-player saves rate column at all
        # (saves is a goalkeeper-specific stat handled separately upstream in
        # model/secondary.py's own Benchwarmers-derived saves projection) --
        # a player with no goalkeeping history simply has no saves prior.

        connection.execute(
            """
            INSERT INTO current_season_player_rate_run VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, 'completed', current_timestamp
            )
            """,
            [
                rate_run_id,
                source_ingestion_run_id,
                season,
                as_of_gameweek,
                as_of,
                SHRINKAGE_PRIOR_MINUTES,
                POLICY_VERSION,
                len(current_rows),
            ],
        )

        insert_rows = []
        for (
            player_code,
            fpl_id,
            position,
            minutes,
            starts,
            xg,
            xa,
            defcon,
            saves,
        ) in current_rows:
            player_code = int(player_code)
            minutes = float(minutes)
            flags: set[str] = set()
            if minutes == 0.0:
                flags.add(ZERO_CURRENT_SEASON_MINUTES)

            xg_xa_prior = prior_rows.get(player_code)
            defcon_prior = secondary_prior_rows.get(player_code)
            if xg_xa_prior is None or defcon_prior is None:
                flags.add(NO_PREVIOUS_SEASON_RATE_HISTORY)
                prior_source = "no_previous_season_history"
                shrunk_xg = _rate_per_90(float(xg), minutes)
                shrunk_xa = _rate_per_90(float(xa), minutes)
                shrunk_defcon = _rate_per_90(float(defcon), minutes)
            else:
                prior_source = "previous_season_player_rate"
                prior_xg_total, prior_xa_total, prior_minutes = xg_xa_prior
                prior_defcon_total, prior_defcon_minutes = defcon_prior
                prior_xg_per_90 = _rate_per_90(prior_xg_total, prior_minutes)
                prior_xa_per_90 = _rate_per_90(prior_xa_total, prior_minutes)
                prior_defcon_per_90 = _rate_per_90(prior_defcon_total, prior_defcon_minutes)
                shrunk_xg = _blend(float(xg), minutes, prior_xg_per_90)
                shrunk_xa = _blend(float(xa), minutes, prior_xa_per_90)
                shrunk_defcon = _blend(float(defcon), minutes, prior_defcon_per_90)
            # Saves has no previous-season per-player prior available in
            # player_rate_history (see note above) -- always the raw
            # current-season rate, flagged so a caller knows no shrinkage
            # was possible for this specific component.
            flags.add("NO_SAVES_SHRINKAGE_PRIOR")
            shrunk_saves = _rate_per_90(float(saves), minutes)

            insert_rows.append(
                (
                    rate_run_id,
                    player_code,
                    int(fpl_id),
                    str(position),
                    int(minutes),
                    int(starts),
                    shrunk_xg,
                    shrunk_xa,
                    shrunk_defcon,
                    shrunk_saves,
                    prior_source,
                    json.dumps(sorted(flags)),
                )
            )
        if insert_rows:
            connection.executemany(
                "INSERT INTO current_season_player_rate VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                insert_rows,
            )

    return CurrentSeasonRateRunResult(
        rate_run_id=rate_run_id,
        source_ingestion_run_id=source_ingestion_run_id,
        season=season,
        as_of_gameweek=as_of_gameweek,
        as_of=as_of,
        final_gameweeks=final_gameweeks,
        player_rows=len(current_rows),
        status="completed",
    )
