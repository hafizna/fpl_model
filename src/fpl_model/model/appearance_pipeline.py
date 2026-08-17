"""Materialise auditable preseason appearance projections from stored inputs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import duckdb

from fpl_model.model.appearance import (
    SeasonAppearanceHistory,
    project_benchwarmers_appearance,
)
from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database

POLICY_VERSION = "benchwarmers_preseason_appearance_v1"


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

        projection_rows = []
        missing = 0
        for fpl_id, player_code, availability, raw_flags in availability_rows:
            flags = set(json.loads(raw_flags))
            player_history = history.get(player_code)
            projection = None
            if availability is None:
                flags.add("UNRESOLVED_AVAILABILITY")
            if player_code is None:
                flags.add("MISSING_PLAYER_CODE")
            elif player_history is None:
                flags.add("NO_WORKBOOK_APPEARANCE_HISTORY")
            if availability is not None and player_history is not None:
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
