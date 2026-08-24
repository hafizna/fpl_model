"""Materialise auditable preseason appearance projections from stored inputs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from pathlib import Path

import duckdb

from fpl_model.model.appearance import (
    DEFAULT_START_MINUTES,
    DEFAULT_SUBSTITUTE_MINUTES,
    AppearanceProjection,
    ConditionalAppearanceScenario,
    MinutesScenario,
    SeasonAppearanceHistory,
    project_appearance,
    project_benchwarmers_appearance,
    project_conditional_appearance,
)
from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database

POLICY_VERSION = "benchwarmers_preseason_appearance_v1"
INSEASON_POLICY_VERSION = "benchwarmers_inseason_appearance_v1"
DEFAULT_PREVIOUS_EFFECTIVE_FIXTURES = 5.0


@dataclass(frozen=True, slots=True)
class AppearanceProjectionRunResult:
    projection_run_id: str
    availability_resolution_run_id: str
    appearance_history_import_run_id: str
    target_gameweek: int
    players: int
    projected_players: int
    missing_players: int
    status: str


def _projection_run_id(availability_run_id: str, history_run_id: str) -> str:
    identity = f"{availability_run_id}|{history_run_id}|{POLICY_VERSION}".encode()
    return f"appearance_{hashlib.sha256(identity).hexdigest()[:16]}"


def _blend_appearance(
    previous: AppearanceProjection,
    current: AppearanceProjection,
    *,
    current_weight: float,
) -> AppearanceProjection:
    previous_weight = 1.0 - current_weight
    values = [
        previous_weight * getattr(previous, field.name)
        + current_weight * getattr(current, field.name)
        for field in fields(AppearanceProjection)
    ]
    return AppearanceProjection(*map(float, values))


def _scale_appearance(
    projection: AppearanceProjection,
    *,
    availability_probability: float,
) -> AppearanceProjection:
    return AppearanceProjection(
        *(
            float(getattr(projection, field.name) * availability_probability)
            for field in fields(AppearanceProjection)
        )
    )


def _projection_values(projection: AppearanceProjection) -> tuple[float, ...]:
    return tuple(
        float(getattr(projection, field.name)) for field in fields(AppearanceProjection)
    )


def materialize_preseason_appearance(
    *,
    target_gameweek: int,
    previous_season: str,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> AppearanceProjectionRunResult:
    """Project GW1 appearance using reviewed history and resolved availability."""
    if target_gameweek != 1:
        raise ValueError(
            "preseason appearance materialisation currently supports GW1 only; "
            "in-season history blending is not implemented"
        )
    initialize_database(database_path)

    with duckdb.connect(str(database_path)) as connection:
        availability_run = connection.execute(
            """
            SELECT resolution_run_id
            FROM availability_resolution_run
            WHERE target_gameweek = ?
            ORDER BY as_of DESC, created_at DESC
            LIMIT 1
            """,
            [target_gameweek],
        ).fetchone()
        if availability_run is None:
            raise ValueError(
                f"no availability resolution found for GW{target_gameweek}"
            )
        availability_run_id = availability_run[0]
        history_run = connection.execute(
            """
            SELECT import_run_id
            FROM appearance_history_import_run
            WHERE season = ? AND status = 'completed'
            ORDER BY imported_at DESC, import_run_id DESC
            LIMIT 1
            """,
            [previous_season],
        ).fetchone()
        if history_run is None:
            raise ValueError(
                f"no appearance history import found for season {previous_season}"
            )
        history_run_id = history_run[0]
        projection_run_id = _projection_run_id(
            availability_run_id,
            history_run_id,
        )

        existing = connection.execute(
            """
            SELECT status FROM appearance_projection_run
            WHERE projection_run_id = ?
            """,
            [projection_run_id],
        ).fetchone()
        if existing is not None:
            counts = connection.execute(
                """
                SELECT count(*), count(start_probability),
                       count(*) - count(start_probability)
                FROM player_appearance_projection
                WHERE projection_run_id = ?
                """,
                [projection_run_id],
            ).fetchone()
            return AppearanceProjectionRunResult(
                projection_run_id,
                availability_run_id,
                history_run_id,
                target_gameweek,
                *map(int, counts),
                existing[0],
            )

        availability_rows = connection.execute(
            """
            SELECT fpl_id, player_code, availability_probability,
                   data_quality_flags
            FROM player_availability_resolution
            WHERE resolution_run_id = ?
            ORDER BY fpl_id
            """,
            [availability_run_id],
        ).fetchall()
        history = {
            row[0]: SeasonAppearanceHistory(
                starts=row[1],
                substitute_appearances=row[2],
                unused_substitute=row[3],
                minutes_per_start=row[4],
                minutes_per_substitute=row[5],
            )
            for row in connection.execute(
                """
                SELECT player_code, starts, substitute_appearances,
                       unused_substitute, minutes_per_start,
                       minutes_per_substitute
                FROM player_appearance_history
                WHERE import_run_id = ?
                """,
                [history_run_id],
            ).fetchall()
        }
        overrides = {
            row[0]: (row[1], ConditionalAppearanceScenario(*row[2:]))
            for row in connection.execute(
                """
                SELECT player_code, override_id,
                       start_probability_if_available,
                       substitute_probability_if_available,
                       sixty_probability_given_start,
                       minutes_per_start, minutes_per_substitute
                FROM appearance_scenario_override
                WHERE target_gameweek = ?
                  AND observed_at <= (
                      SELECT as_of FROM availability_resolution_run
                      WHERE resolution_run_id = ?
                  )
                  AND (
                      effective_until IS NULL OR effective_until >= (
                          SELECT as_of FROM availability_resolution_run
                          WHERE resolution_run_id = ?
                      )
                  )
                QUALIFY row_number() OVER (
                    PARTITION BY player_code ORDER BY observed_at DESC, override_id DESC
                ) = 1
                """,
                [target_gameweek, availability_run_id, availability_run_id],
            ).fetchall()
        }

        projection_rows = []
        missing = 0
        for fpl_id, player_code, availability, raw_flags in availability_rows:
            flags = set(json.loads(raw_flags))
            player_history = history.get(player_code)
            reviewed_override = overrides.get(player_code)
            projection = None
            if availability is None:
                flags.add("UNRESOLVED_AVAILABILITY")
            if player_code is None:
                flags.add("MISSING_PLAYER_CODE")
            elif player_history is None and reviewed_override is None:
                flags.add("NO_WORKBOOK_APPEARANCE_HISTORY")
            if availability is not None and reviewed_override is not None:
                override_id, scenario = reviewed_override
                projection = project_conditional_appearance(
                    scenario,
                    availability_probability=availability,
                )
                flags.add("REVIEWED_APPEARANCE_SCENARIO_OVERRIDE")
                flags.add(f"APPEARANCE_SCENARIO_OVERRIDE_ID={override_id}")
            elif availability is not None and player_history is not None:
                projection = project_benchwarmers_appearance(
                    player_history,
                    SeasonAppearanceHistory(0, 0, 0),
                    availability_probability=availability,
                    appearance_previous_weight=1.0,
                    sixty_previous_weight=1.0,
                )
            else:
                missing += 1

            projection_rows.append(
                (
                    projection_run_id,
                    fpl_id,
                    player_code,
                    availability,
                    projection.start_probability if projection else None,
                    (
                        projection.substitute_appearance_probability
                        if projection
                        else None
                    ),
                    projection.appearance_probability if projection else None,
                    projection.sixty_minute_probability if projection else None,
                    projection.expected_minutes if projection else None,
                    projection.appearance_xpts if projection else None,
                    projection.sixty_minute_xpts if projection else None,
                    projection.total_xpts if projection else None,
                    json.dumps(sorted(flags)),
                )
            )

        status = "completed_with_gaps" if missing else "completed"
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT INTO appearance_projection_run (
                    projection_run_id, availability_resolution_run_id,
                    appearance_history_import_run_id, target_gameweek,
                    policy_version, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    projection_run_id,
                    availability_run_id,
                    history_run_id,
                    target_gameweek,
                    POLICY_VERSION,
                    status,
                ],
            )
            connection.executemany(
                "INSERT INTO player_appearance_projection VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                projection_rows,
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    return AppearanceProjectionRunResult(
        projection_run_id=projection_run_id,
        availability_resolution_run_id=availability_run_id,
        appearance_history_import_run_id=history_run_id,
        target_gameweek=target_gameweek,
        players=len(projection_rows),
        projected_players=len(projection_rows) - missing,
        missing_players=missing,
        status=status,
    )


def materialize_inseason_appearance(
    *,
    target_gameweek: int,
    current_season: str,
    previous_season: str,
    availability_resolution_run_id: str | None = None,
    previous_effective_fixtures: float = DEFAULT_PREVIOUS_EFFECTIVE_FIXTURES,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> AppearanceProjectionRunResult:
    """Blend final prior-GW event history with the reviewed previous-season prior."""
    if not 2 <= target_gameweek <= 38:
        raise ValueError("in-season appearance target_gameweek must be between 2 and 38")
    if previous_effective_fixtures <= 0.0:
        raise ValueError("previous_effective_fixtures must be positive")
    initialize_database(database_path)

    with duckdb.connect(str(database_path)) as connection:
        availability_parameters: list[object] = [target_gameweek]
        availability_filter = ""
        if availability_resolution_run_id is not None:
            availability_filter = "AND resolution_run_id = ?"
            availability_parameters.append(availability_resolution_run_id)
        availability_run = connection.execute(
            f"""
            SELECT resolution_run_id, source_ingestion_run_id, as_of, deadline
            FROM availability_resolution_run
            WHERE target_gameweek = ? AND status IN ('completed', 'completed_with_gaps')
              {availability_filter}
            ORDER BY as_of DESC, created_at DESC LIMIT 1
            """,
            availability_parameters,
        ).fetchone()
        if availability_run is None:
            raise ValueError(f"no availability resolution found for GW{target_gameweek}")
        availability_run_id, source_ingestion_run_id, availability_as_of, deadline = (
            availability_run
        )

        history_run = connection.execute(
            """
            SELECT import_run_id FROM appearance_history_import_run
            WHERE season = ? AND status = 'completed'
            ORDER BY imported_at DESC, import_run_id DESC LIMIT 1
            """,
            [previous_season],
        ).fetchone()
        if history_run is None:
            raise ValueError(
                f"no appearance history import found for season {previous_season}"
            )
        history_run_id = str(history_run[0])

        live_runs = connection.execute(
            """
            SELECT live_run_id, gameweek, captured_at
            FROM fpl_event_live_run
            WHERE season = ? AND gameweek < ? AND status = 'completed'
              AND event_finished AND data_checked AND captured_at <= ?
            QUALIFY row_number() OVER (
                PARTITION BY gameweek ORDER BY captured_at DESC, live_run_id DESC
            ) = 1
            ORDER BY gameweek
            """,
            [current_season, target_gameweek, deadline],
        ).fetchall()
        actual_gameweeks = [int(row[1]) for row in live_runs]
        expected_gameweeks = list(range(1, target_gameweek))
        if actual_gameweeks != expected_gameweeks:
            missing = sorted(set(expected_gameweeks) - set(actual_gameweeks))
            raise ValueError(
                f"final event-live history is incomplete before GW{target_gameweek}: "
                f"missing={missing}"
            )
        live_run_ids = [str(row[0]) for row in live_runs]
        as_of = max([availability_as_of, *(row[2] for row in live_runs)])
        if as_of > deadline:
            raise ValueError("in-season appearance evidence was captured after the deadline")

        identity = json.dumps(
            {
                "availability_run_id": availability_run_id,
                "history_run_id": history_run_id,
                "live_run_ids": live_run_ids,
                "previous_effective_fixtures": previous_effective_fixtures,
                "policy_version": INSEASON_POLICY_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        projection_run_id = f"appearance_{hashlib.sha256(identity).hexdigest()[:16]}"
        existing = connection.execute(
            "SELECT status FROM appearance_projection_run WHERE projection_run_id = ?",
            [projection_run_id],
        ).fetchone()
        if existing is not None:
            counts = connection.execute(
                """
                SELECT count(*), count(start_probability),
                       count(*) - count(start_probability)
                FROM player_appearance_projection WHERE projection_run_id = ?
                """,
                [projection_run_id],
            ).fetchone()
            return AppearanceProjectionRunResult(
                projection_run_id,
                str(availability_run_id),
                history_run_id,
                target_gameweek,
                *map(int, counts),
                str(existing[0]),
            )

        availability_rows = connection.execute(
            """
            SELECT fpl_id, player_code, availability_probability, data_quality_flags
            FROM player_availability_resolution
            WHERE resolution_run_id = ? ORDER BY fpl_id
            """,
            [availability_run_id],
        ).fetchall()
        previous_history = {
            int(row[0]): SeasonAppearanceHistory(
                starts=int(row[1]),
                substitute_appearances=int(row[2]),
                unused_substitute=int(row[3]),
                minutes_per_start=float(row[4]),
                minutes_per_substitute=float(row[5]),
            )
            for row in connection.execute(
                """
                SELECT player_code, starts, substitute_appearances,
                       unused_substitute, minutes_per_start, minutes_per_substitute
                FROM player_appearance_history WHERE import_run_id = ?
                """,
                [history_run_id],
            ).fetchall()
        }
        overrides = {
            int(row[0]): (str(row[1]), ConditionalAppearanceScenario(*row[2:]))
            for row in connection.execute(
                """
                SELECT player_code, override_id,
                       start_probability_if_available,
                       substitute_probability_if_available,
                       sixty_probability_given_start,
                       minutes_per_start, minutes_per_substitute
                FROM appearance_scenario_override
                WHERE target_gameweek = ? AND observed_at <= ?
                  AND (effective_until IS NULL OR effective_until >= ?)
                QUALIFY row_number() OVER (
                    PARTITION BY player_code ORDER BY observed_at DESC, override_id DESC
                ) = 1
                """,
                [target_gameweek, as_of, as_of],
            ).fetchall()
        }

        placeholders = ", ".join("?" for _ in live_run_ids)
        live_rows = connection.execute(
            f"""
            SELECT s.player_code, r.gameweek, s.minutes, s.starts,
                   ps.team_join_date, gw.deadline_time
            FROM player_gameweek_stat AS s
            JOIN fpl_event_live_run AS r USING (live_run_id)
            JOIN player_status_snapshot AS ps
              ON ps.ingestion_run_id = r.source_ingestion_run_id
             AND ps.fpl_id = s.fpl_id
            JOIN gameweek_snapshot AS gw
              ON gw.ingestion_run_id = r.source_ingestion_run_id
             AND gw.gameweek = r.gameweek
            WHERE s.live_run_id IN ({placeholders}) AND s.player_code IS NOT NULL
            ORDER BY s.player_code, r.gameweek
            """,
            live_run_ids,
        ).fetchall()
        current_rows: dict[int, list[tuple[int, int]]] = {}
        for player_code, _, minutes, starts, team_join_date, event_deadline in live_rows:
            if team_join_date is not None and team_join_date > event_deadline.date():
                continue
            current_rows.setdefault(int(player_code), []).append(
                (int(minutes), int(starts))
            )

        projection_rows = []
        context_rows = []
        missing = 0
        for fpl_id, player_code, availability, raw_flags in availability_rows:
            flags = set(json.loads(raw_flags))
            projection = None
            previous_weight = 0.0
            current_weight = 1.0
            start_minutes = DEFAULT_START_MINUTES
            substitute_minutes = DEFAULT_SUBSTITUTE_MINUTES
            samples = current_rows.get(int(player_code), []) if player_code else []
            reviewed_override = overrides.get(int(player_code)) if player_code else None

            if availability is None:
                flags.add("UNRESOLVED_AVAILABILITY")
            elif reviewed_override is not None:
                override_id, scenario = reviewed_override
                projection = project_conditional_appearance(
                    scenario,
                    availability_probability=float(availability),
                )
                start_minutes = scenario.minutes_per_start
                substitute_minutes = scenario.minutes_per_substitute
                flags.add("REVIEWED_APPEARANCE_SCENARIO_OVERRIDE")
                flags.add(f"APPEARANCE_SCENARIO_OVERRIDE_ID={override_id}")
            else:
                old_history = previous_history.get(int(player_code)) if player_code else None
                old_projection = (
                    project_benchwarmers_appearance(
                        old_history,
                        SeasonAppearanceHistory(0, 0, 0),
                        availability_probability=1.0,
                        appearance_previous_weight=1.0,
                        sixty_previous_weight=1.0,
                    )
                    if old_history is not None
                    else None
                )
                invalid_aggregate = any(minutes > 90 or starts > 1 for minutes, starts in samples)
                if invalid_aggregate:
                    flags.add("UNSUPPORTED_EVENT_AGGREGATE_DGW")
                    samples = []
                current_projection = None
                current_start_minutes = DEFAULT_START_MINUTES
                current_substitute_minutes = DEFAULT_SUBSTITUTE_MINUTES
                if samples:
                    probability = 1.0 / len(samples)
                    scenarios = tuple(
                        MinutesScenario(
                            probability=probability,
                            minutes=float(minutes),
                            started=bool(starts),
                            label="current_season_final_event",
                        )
                        for minutes, starts in samples
                    )
                    current_projection = project_appearance(scenarios)
                    start_values = [m for m, starts in samples if starts]
                    substitute_values = [m for m, starts in samples if not starts and m > 0]
                    if start_values:
                        current_start_minutes = sum(start_values) / len(start_values)
                    if substitute_values:
                        current_substitute_minutes = sum(substitute_values) / len(
                            substitute_values
                        )
                if old_projection is not None and current_projection is not None:
                    current_weight = len(samples) / (
                        len(samples) + previous_effective_fixtures
                    )
                    previous_weight = 1.0 - current_weight
                    projection = _blend_appearance(
                        old_projection,
                        current_projection,
                        current_weight=current_weight,
                    )
                    start_minutes = (
                        previous_weight
                        * (old_history.minutes_per_start or DEFAULT_START_MINUTES)
                        + current_weight * current_start_minutes
                    )
                    substitute_minutes = (
                        previous_weight
                        * (
                            old_history.minutes_per_substitute
                            or DEFAULT_SUBSTITUTE_MINUTES
                        )
                        + current_weight * current_substitute_minutes
                    )
                    flags.add("SHRUNK_CURRENT_SEASON_APPEARANCE")
                elif current_projection is not None:
                    projection = current_projection
                    start_minutes = current_start_minutes
                    substitute_minutes = current_substitute_minutes
                    flags.add("CURRENT_SEASON_APPEARANCE_ONLY")
                elif old_projection is not None:
                    projection = old_projection
                    previous_weight = 1.0
                    current_weight = 0.0
                    start_minutes = old_history.minutes_per_start or DEFAULT_START_MINUTES
                    substitute_minutes = (
                        old_history.minutes_per_substitute or DEFAULT_SUBSTITUTE_MINUTES
                    )
                    flags.add("NO_CURRENT_SEASON_APPEARANCE_SAMPLE")
                else:
                    flags.add("MISSING_APPEARANCE_HISTORY")
                if projection is not None:
                    projection = _scale_appearance(
                        projection,
                        availability_probability=float(availability),
                    )

            if projection is None:
                missing += 1
            projection_rows.append(
                (
                    projection_run_id,
                    fpl_id,
                    player_code,
                    availability,
                    *(_projection_values(projection) if projection else (None,) * 8),
                    json.dumps(sorted(flags)),
                )
            )
            context_rows.append(
                (
                    projection_run_id,
                    fpl_id,
                    len(samples),
                    previous_weight,
                    current_weight,
                    start_minutes,
                    substitute_minutes,
                    json.dumps(sorted(flags)),
                )
            )

        status = "completed_with_gaps" if missing else "completed"
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT INTO appearance_projection_run (
                    projection_run_id, availability_resolution_run_id,
                    appearance_history_import_run_id, target_gameweek,
                    policy_version, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    projection_run_id,
                    availability_run_id,
                    history_run_id,
                    target_gameweek,
                    INSEASON_POLICY_VERSION,
                    status,
                ],
            )
            connection.execute(
                """
                INSERT INTO inseason_appearance_run VALUES (
                    ?, ?, ?, 1, ?, ?, ?, ?, ?
                )
                """,
                [
                    projection_run_id,
                    current_season,
                    previous_season,
                    target_gameweek - 1,
                    json.dumps(live_run_ids),
                    previous_effective_fixtures,
                    as_of,
                    INSEASON_POLICY_VERSION,
                ],
            )
            connection.executemany(
                "INSERT INTO player_appearance_projection VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                projection_rows,
            )
            connection.executemany(
                "INSERT INTO inseason_player_appearance_context VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?)",
                context_rows,
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    return AppearanceProjectionRunResult(
        projection_run_id=projection_run_id,
        availability_resolution_run_id=str(availability_run_id),
        appearance_history_import_run_id=history_run_id,
        target_gameweek=target_gameweek,
        players=len(projection_rows),
        projected_players=len(projection_rows) - missing,
        missing_players=missing,
        status=status,
    )
