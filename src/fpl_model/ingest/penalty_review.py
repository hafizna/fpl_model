"""Reviewed penalty decomposition for final official FPL event-live rows."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb

from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database


@dataclass(frozen=True, slots=True)
class PlayerPenaltyEvent:
    fpl_id: int
    attempts: int
    goals: int
    penalty_xg: float

    def __post_init__(self) -> None:
        if self.fpl_id < 1:
            raise ValueError("fpl_id must be positive")
        if self.attempts < 1:
            raise ValueError("penalty attempts must be positive")
        if not 0 <= self.goals <= self.attempts:
            raise ValueError("penalty goals must be between zero and attempts")
        if self.penalty_xg < 0.0:
            raise ValueError("penalty_xg must be non-negative")


@dataclass(frozen=True, slots=True)
class PenaltyReviewResult:
    review_id: str
    live_run_id: str
    player_rows: int
    penalty_takers: int


def store_event_penalty_review(
    *,
    live_run_id: str,
    observed_at: datetime,
    source_reference: str,
    rationale: str,
    penalty_events: tuple[PlayerPenaltyEvent, ...],
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> PenaltyReviewResult:
    """Declare a complete reviewed event penalty ledger and derive npxG.

    An empty ``penalty_events`` tuple is meaningful: the reviewer is asserting
    that the completed Gameweek contained no penalties. Without a completed
    review, no non-penalty xG row is materialised at all.
    """
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if not live_run_id.strip() or not source_reference.strip() or not rationale.strip():
        raise ValueError("review identifiers, source, and rationale must not be blank")
    if len({row.fpl_id for row in penalty_events}) != len(penalty_events):
        raise ValueError("penalty_events contains duplicate fpl_id values")
    event_payload = [
        {
            "fpl_id": row.fpl_id,
            "attempts": row.attempts,
            "goals": row.goals,
            "penalty_xg": row.penalty_xg,
        }
        for row in sorted(penalty_events, key=lambda item: item.fpl_id)
    ]
    identity = json.dumps(
        {
            "live_run_id": live_run_id,
            "observed_at": observed_at.isoformat(),
            "source_reference": source_reference,
            "rationale": rationale,
            "penalty_events": event_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    review_id = f"penalty_review_{hashlib.sha256(identity).hexdigest()[:16]}"
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        live = connection.execute(
            """
            SELECT status, captured_at FROM fpl_event_live_run WHERE live_run_id = ?
            """,
            [live_run_id],
        ).fetchone()
        if live is None or live[0] != "completed":
            raise ValueError("penalty review requires a completed final event-live run")
        if observed_at < live[1]:
            raise ValueError("penalty review cannot be observed before the event-live capture")
        stats = {
            int(row[0]): (float(row[1]), int(row[2]))
            for row in connection.execute(
                """
                SELECT fpl_id, expected_goals, goals_scored
                FROM player_gameweek_stat WHERE live_run_id = ?
                """,
                [live_run_id],
            ).fetchall()
        }
        if not stats:
            raise ValueError("event-live run has no player statistics")
        events = {row.fpl_id: row for row in penalty_events}
        unknown = sorted(set(events) - set(stats))
        if unknown:
            raise ValueError(f"penalty review contains unknown fpl_ids: {unknown}")
        for fpl_id, event in events.items():
            total_xg, total_goals = stats[fpl_id]
            if event.penalty_xg > total_xg + 1e-6:
                raise ValueError("penalty_xg cannot exceed the player's total expected_goals")
            if event.goals > total_goals:
                raise ValueError("penalty goals cannot exceed the player's total goals")
        existing = connection.execute(
            "SELECT review_id FROM event_penalty_review WHERE live_run_id = ?",
            [live_run_id],
        ).fetchone()
        if existing is not None:
            if existing[0] != review_id:
                raise ValueError("event-live run already has a different complete penalty review")
            count = connection.execute(
                "SELECT count(*) FROM player_gameweek_attacking_decomposition WHERE live_run_id = ?",
                [live_run_id],
            ).fetchone()[0]
            return PenaltyReviewResult(review_id, live_run_id, int(count), len(events))
        decomposition = []
        for fpl_id, (total_xg, _) in sorted(stats.items()):
            event = events.get(fpl_id)
            penalty_xg = event.penalty_xg if event else 0.0
            attempts = event.attempts if event else 0
            goals = event.goals if event else 0
            flags = (
                ["REVIEWED_PENALTY_DECOMPOSITION"]
                if event
                else ["COMPLETE_REVIEW_NO_PLAYER_PENALTY"]
            )
            decomposition.append(
                (
                    live_run_id,
                    fpl_id,
                    review_id,
                    total_xg,
                    penalty_xg,
                    max(0.0, total_xg - penalty_xg),
                    attempts,
                    goals,
                    json.dumps(flags),
                )
            )
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT INTO event_penalty_review VALUES (
                    ?, ?, ?, ?, ?, 'completed', current_timestamp
                )
                """,
                [review_id, live_run_id, observed_at, source_reference, rationale],
            )
            if penalty_events:
                connection.executemany(
                    "INSERT INTO player_penalty_event VALUES (?, ?, ?, ?, ?)",
                    [
                        (review_id, row.fpl_id, row.attempts, row.goals, row.penalty_xg)
                        for row in penalty_events
                    ],
                )
            connection.executemany(
                "INSERT INTO player_gameweek_attacking_decomposition VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                decomposition,
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")
    return PenaltyReviewResult(review_id, live_run_id, len(decomposition), len(events))
