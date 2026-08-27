"""Detect a material mismatch between a projection and its own realised outcome.

P0 (`README.md`'s "Production critical path") names two concrete conflict
shapes a manager should never see silently absorbed into an aggregate error
metric: "a 60+ minute start with low projected start probability, or a
zero-minute available player with a high projected appearance probability".
This module detects exactly those two shapes, per player, for one completed
model run compared against the SAME Gameweek's own final official event data.

This is deliberately retrospective and read-only: it compares one immutable
`model_run`'s stored projection against one immutable, FINAL
`fpl_event_live_run`'s stored outcome. It changes no projection, no
calibration, and no recommendation -- it is an audit layer a manager (or the
prospective decision-safety layer built on top of it later) can use to see
where a specific player's projection materially diverged from what actually
happened, the same way `validation/appearance_segments.py` diagnoses
aggregate bias but at the single-player, single-Gameweek grain instead.

Only a FINAL event (`event_finished AND data_checked`) is compared against --
see `docs/INSEASON_REFRESH.md`'s own finality boundary. Comparing against a
provisional event would risk a false "conflict" purely from FPL not having
finished checking the Gameweek yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import duckdb

from fpl_model.validation.role_state import LIKELY_STARTER_THRESHOLD, ROTATION_THRESHOLD

UNEXPECTED_SUBSTANTIAL_START = "unexpected_substantial_start"
UNEXPECTED_BLANK = "unexpected_blank"

# "60+ minute start" is the roadmap's own literal example.
SUBSTANTIAL_START_MINUTES = 60
# "low projected start probability" for the first conflict shape: reuses
# role_state's own ROTATION_THRESHOLD, so "low" means the same number a
# manager already sees as "not LIKELY_STARTER" in the role state itself.
LOW_START_PROBABILITY_THRESHOLD = ROTATION_THRESHOLD
# "high projected appearance probability" for the second conflict shape:
# reuses role_state's own LIKELY_STARTER_THRESHOLD as "high enough that a
# total blank should have been unlikely", not merely "not LIKELY_BENCH".
HIGH_APPEARANCE_PROBABILITY_THRESHOLD = LIKELY_STARTER_THRESHOLD


@dataclass(frozen=True, slots=True)
class MaterialConflict:
    fpl_id: int
    conflict_type: str
    projected_start_probability: float
    projected_appearance_probability: float
    actual_minutes: int
    actual_started: bool
    reason: str

    def __post_init__(self) -> None:
        if self.conflict_type not in (UNEXPECTED_SUBSTANTIAL_START, UNEXPECTED_BLANK):
            raise ValueError(f"unknown conflict_type: {self.conflict_type!r}")


def detect_conflict(
    *,
    projected_start_probability: float,
    projected_appearance_probability: float,
    actual_minutes: int,
    actual_started: bool,
) -> str | None:
    """Return a conflict type, or ``None`` when the outcome is unremarkable.

    Pure function: no I/O, no player identity -- just the comparison rule, so
    it can be unit-tested against the roadmap's own two named shapes directly.
    A player can trigger at most one conflict type; the two shapes are
    mutually exclusive by construction (a 60+ minute start cannot also be a
    zero-minute blank).
    """
    if actual_minutes >= SUBSTANTIAL_START_MINUTES:
        if projected_start_probability < LOW_START_PROBABILITY_THRESHOLD:
            return UNEXPECTED_SUBSTANTIAL_START
        return None
    if actual_minutes == 0:
        if projected_appearance_probability >= HIGH_APPEARANCE_PROBABILITY_THRESHOLD:
            return UNEXPECTED_BLANK
        return None
    return None


def _conflict_reason(
    conflict_type: str,
    *,
    projected_start_probability: float,
    projected_appearance_probability: float,
    actual_minutes: int,
) -> str:
    if conflict_type == UNEXPECTED_SUBSTANTIAL_START:
        return (
            f"Started and played {actual_minutes} minutes despite a projected start "
            f"probability of only {projected_start_probability:.0%}."
        )
    return (
        f"Played 0 minutes despite a projected appearance probability of "
        f"{projected_appearance_probability:.0%}."
    )


def audit_material_conflicts(
    connection: duckdb.DuckDBPyConnection,
    *,
    model_run_id: str,
    live_run_id: str,
) -> tuple[MaterialConflict, ...]:
    """Compare one model run's own projections against one final event-live run.

    Both runs must target the SAME Gameweek and share the SAME official FPL
    snapshot lineage; a mismatch raises rather than silently comparing
    unrelated data. ``live_run_id`` must be FINAL (`event_finished AND
    data_checked`) -- comparing against a provisional run raises, since FPL
    has not finished checking that Gameweek's outcomes yet.
    """
    model = connection.execute(
        "SELECT target_gameweek, source_ingestion_run_id FROM model_run WHERE model_run_id = ?",
        [model_run_id],
    ).fetchone()
    if model is None:
        raise ValueError(f"unknown model_run_id: {model_run_id}")
    target_gameweek, source_ingestion_run_id = model

    live_run = connection.execute(
        """
        SELECT gameweek, source_ingestion_run_id, event_finished, data_checked
        FROM fpl_event_live_run
        WHERE live_run_id = ?
        """,
        [live_run_id],
    ).fetchone()
    if live_run is None:
        raise ValueError(f"unknown live_run_id: {live_run_id}")
    live_gameweek, live_source_run, event_finished, data_checked = live_run
    if int(live_gameweek) != int(target_gameweek):
        raise ValueError(
            f"model run targets GW{target_gameweek} but event-live run is for "
            f"GW{live_gameweek}"
        )
    if not (event_finished and data_checked):
        raise ValueError(
            f"event-live run {live_run_id} is not final "
            f"(event_finished={event_finished}, data_checked={data_checked})"
        )
    if str(live_source_run) != str(source_ingestion_run_id):
        raise ValueError(
            "model run and event-live run use different official FPL snapshots: "
            f"{source_ingestion_run_id!r} vs {live_source_run!r}"
        )

    rows = connection.execute(
        """
        SELECT s.fpl_id, s.minutes, s.starts,
               sum(p.start_probability) AS start_probability,
               sum(p.start_probability + p.substitute_appearance_probability)
                   AS appearance_probability
        FROM player_gameweek_stat AS s
        JOIN player_fixture_projection AS p
          ON p.player_code = s.player_code
        WHERE s.live_run_id = ? AND p.model_run_id = ? AND s.player_code IS NOT NULL
        -- player_gameweek_stat is already one row per player per live_run_id
        -- (PRIMARY KEY (live_run_id, fpl_id): minutes/starts are already the
        -- whole-Gameweek total, including a double Gameweek). Only
        -- player_fixture_projection is genuinely per-fixture and needs the
        -- sum() above; grouping by every selected non-aggregate column keeps
        -- the query an explicit functional dependency rather than relying on
        -- the primary key implicitly.
        GROUP BY s.fpl_id, s.minutes, s.starts
        """,
        [live_run_id, model_run_id],
    ).fetchall()

    conflicts: list[MaterialConflict] = []
    for fpl_id, minutes, starts, start_probability, appearance_probability in rows:
        # A double-Gameweek's per-fixture start_probability values are summed
        # above, which can exceed 1.0 across two fixtures -- clamp only for
        # the comparison threshold, never for the stored/reported value,
        # since a >1.0 sum is itself informative (this player had two
        # start opportunities the single-fixture threshold was not designed
        # for) rather than an error to hide.
        clamped_start = min(1.0, float(start_probability))
        clamped_appearance = min(1.0, float(appearance_probability))
        conflict_type = detect_conflict(
            projected_start_probability=clamped_start,
            projected_appearance_probability=clamped_appearance,
            actual_minutes=int(minutes),
            actual_started=bool(starts),
        )
        if conflict_type is None:
            continue
        conflicts.append(
            MaterialConflict(
                fpl_id=int(fpl_id),
                conflict_type=conflict_type,
                projected_start_probability=float(start_probability),
                projected_appearance_probability=float(appearance_probability),
                actual_minutes=int(minutes),
                actual_started=bool(starts),
                reason=_conflict_reason(
                    conflict_type,
                    projected_start_probability=float(start_probability),
                    projected_appearance_probability=float(appearance_probability),
                    actual_minutes=int(minutes),
                ),
            )
        )
    return tuple(sorted(conflicts, key=lambda row: row.fpl_id))


def material_conflict_report(conflicts: tuple[MaterialConflict, ...]) -> list[dict[str, Any]]:
    """Render conflicts as a JSON-serialisable list."""
    return [
        {
            "fpl_id": row.fpl_id,
            "conflict_type": row.conflict_type,
            "projected_start_probability": row.projected_start_probability,
            "projected_appearance_probability": row.projected_appearance_probability,
            "actual_minutes": row.actual_minutes,
            "actual_started": row.actual_started,
            "reason": row.reason,
        }
        for row in conflicts
    ]
