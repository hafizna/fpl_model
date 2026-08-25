"""Deadline-safe materialisation of descriptive Sprint 3 context features.

These features are stored for calibration and ablation. They deliberately do
not modify expected points until an out-of-sample test supports that change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

from fpl_model.context.congestion import PriorAppearance, workload_features
from fpl_model.context.readiness import TournamentReadiness
from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database
from fpl_model.tactics.roles import RoleVector, role_distance

POLICY_VERSION = "deadline_safe_context_features_v1"


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ReviewedContextAnnotation:
    subject_type: str
    context_type: str
    observed_at: datetime
    effective_from: datetime
    payload: dict[str, Any]
    source_reference: str
    rationale: str
    player_code: int | None = None
    team_id: int | None = None
    effective_until: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("observed_at", "effective_from"):
            _aware(getattr(self, name), name)
        if self.effective_until is not None:
            _aware(self.effective_until, "effective_until")
            if self.effective_until < self.effective_from:
                raise ValueError("effective_until cannot be before effective_from")
        if self.subject_type == "player":
            if self.player_code is None or self.team_id is not None:
                raise ValueError("player annotation requires only player_code")
        elif self.subject_type == "team":
            if self.team_id is None or self.player_code is not None:
                raise ValueError("team annotation requires only team_id")
        else:
            raise ValueError("subject_type must be player or team")
        expected_subject = {
            "manager_regime": "team",
            "readiness": "player",
            "tactical_role": "player",
        }.get(self.context_type)
        if expected_subject is None:
            raise ValueError("unsupported context_type")
        if self.subject_type != expected_subject:
            raise ValueError(f"{self.context_type} must use a {expected_subject} subject")
        if not self.source_reference.strip() or not self.rationale.strip():
            raise ValueError("source_reference and rationale must not be blank")
        _validate_payload(self.context_type, self.payload)


@dataclass(frozen=True, slots=True)
class ContextFeatureRunResult:
    context_run_id: str
    appearance_projection_run_id: str
    target_gameweek: int
    player_rows: int
    fully_observed_rows: int
    status: str


def _parse_date(value: object, name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date") from exc


def _role(payload: dict[str, Any]) -> RoleVector:
    return RoleVector(
        width=float(payload["width"]),
        height=float(payload["height"]),
        centrality=float(payload["centrality"]),
        build_up=float(payload["build_up"]),
        box_presence=float(payload["box_presence"]),
        defensive_load=float(payload["defensive_load"]),
    )


def _validate_payload(context_type: str, payload: dict[str, Any]) -> None:
    if context_type == "manager_regime":
        if not str(payload.get("manager_name", "")).strip():
            raise ValueError("manager_regime requires manager_name")
        _parse_date(payload.get("regime_start"), "regime_start")
    elif context_type == "readiness":
        for name in ("tournament_minutes", "preseason_minutes"):
            if float(payload.get(name, 0.0)) < 0.0:
                raise ValueError(f"{name} must be non-negative")
        for name in ("last_tournament_match", "club_return_date"):
            if payload.get(name) is not None:
                _parse_date(payload[name], name)
    elif context_type == "tactical_role":
        if not str(payload.get("role_label", "")).strip():
            raise ValueError("tactical_role requires role_label")
        if not str(payload.get("nominal_position", "")).strip():
            raise ValueError("tactical_role requires nominal_position")
        _role(payload)


def store_reviewed_context_annotation(
    annotation: ReviewedContextAnnotation,
    *,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> str:
    """Store one immutable, content-addressed reviewed context annotation."""
    initialize_database(database_path)
    payload_text = json.dumps(annotation.payload, sort_keys=True, separators=(",", ":"))
    identity = json.dumps(
        {
            "subject_type": annotation.subject_type,
            "player_code": annotation.player_code,
            "team_id": annotation.team_id,
            "context_type": annotation.context_type,
            "observed_at": annotation.observed_at.isoformat(),
            "effective_from": annotation.effective_from.isoformat(),
            "effective_until": (
                annotation.effective_until.isoformat()
                if annotation.effective_until is not None
                else None
            ),
            "payload": annotation.payload,
            "source_reference": annotation.source_reference,
            "rationale": annotation.rationale,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    annotation_id = f"context_{hashlib.sha256(identity).hexdigest()[:16]}"
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO reviewed_context_annotation (
                annotation_id, subject_type, player_code, team_id, context_type,
                observed_at, effective_from, effective_until, payload,
                source_reference, rationale
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            [
                annotation_id,
                annotation.subject_type,
                annotation.player_code,
                annotation.team_id,
                annotation.context_type,
                annotation.observed_at,
                annotation.effective_from,
                annotation.effective_until,
                payload_text,
                annotation.source_reference,
                annotation.rationale,
            ],
        )
    return annotation_id


def _latest_current(
    rows: list[tuple[Any, ...]], *, deadline: datetime
) -> tuple[Any, ...] | None:
    eligible = [row for row in rows if row[3] is None or row[3] >= deadline]
    return max(eligible, key=lambda row: (row[2], row[1], row[0])) if eligible else None


def materialize_context_features(
    *,
    target_gameweek: int,
    appearance_projection_run_id: str | None = None,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> ContextFeatureRunResult:
    """Materialise causal manager/readiness/workload/role diagnostics for GW2+."""
    if not 2 <= target_gameweek <= 38:
        raise ValueError("context target_gameweek must be between 2 and 38")
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        parameters: list[object] = [target_gameweek]
        run_filter = ""
        if appearance_projection_run_id is not None:
            run_filter = "AND iar.projection_run_id = ?"
            parameters.append(appearance_projection_run_id)
        appearance = connection.execute(
            f"""
            SELECT iar.projection_run_id, ar.source_ingestion_run_id, iar.as_of,
                   ar.deadline, iar.live_run_ids
            FROM inseason_appearance_run AS iar
            JOIN appearance_projection_run AS apr USING (projection_run_id)
            JOIN availability_resolution_run AS ar
              ON ar.resolution_run_id = apr.availability_resolution_run_id
            WHERE apr.target_gameweek = ? {run_filter}
            ORDER BY iar.as_of DESC, iar.projection_run_id DESC LIMIT 1
            """,
            parameters,
        ).fetchone()
        if appearance is None:
            raise ValueError(f"no in-season appearance run found for GW{target_gameweek}")
        appearance_run, source_run, appearance_as_of, deadline, live_ids_text = appearance
        live_run_ids = list(json.loads(live_ids_text))
        live_quality_flags: set[str] = set()
        if live_run_ids:
            placeholders = ", ".join("?" for _ in live_run_ids)
            if connection.execute(
                f"""
                SELECT count(*) FROM fpl_event_live_run
                WHERE live_run_id IN ({placeholders}) AND status = 'provisional'
                """,
                live_run_ids,
            ).fetchone()[0]:
                live_quality_flags.add(
                    "OFFICIAL_EVENT_ANALYTICALLY_COMPLETE_NOT_FINAL"
                )

        annotations = connection.execute(
            """
            SELECT annotation_id, observed_at, effective_from, effective_until,
                   subject_type, player_code, team_id, context_type, payload
            FROM reviewed_context_annotation
            WHERE observed_at <= ? AND effective_from <= ?
            ORDER BY effective_from, observed_at, annotation_id
            """,
            [deadline, deadline],
        ).fetchall()
        by_subject: dict[tuple[str, int, str], list[tuple[Any, ...]]] = {}
        for row in annotations:
            subject_id = int(row[5] if row[4] == "player" else row[6])
            by_subject.setdefault((str(row[4]), subject_id, str(row[7])), []).append(
                (str(row[0]), row[1], row[2], row[3], json.loads(row[8]))
            )

        selected_annotation_ids: set[str] = set()
        selected_observed_at = [appearance_as_of]
        players = connection.execute(
            """
            SELECT fpl_id, player_code, team_id, fpl_position
            FROM player_snapshot WHERE ingestion_run_id = ? ORDER BY fpl_id
            """,
            [source_run],
        ).fetchall()
        previous_deadline = connection.execute(
            """
            SELECT deadline_time FROM gameweek_snapshot
            WHERE ingestion_run_id = ? AND gameweek = ?
            """,
            [source_run, target_gameweek - 1],
        ).fetchone()
        previous_deadline = previous_deadline[0] if previous_deadline else None

        appearances: dict[int, list[PriorAppearance]] = {}
        congestion_flags: dict[int, set[str]] = {}
        if live_run_ids:
            placeholders = ", ".join("?" for _ in live_run_ids)
            history_rows = connection.execute(
                f"""
                SELECT r.live_run_id, r.gameweek, r.source_ingestion_run_id,
                       s.player_code, s.minutes, ps.team_id
                FROM player_gameweek_stat AS s
                JOIN fpl_event_live_run AS r USING (live_run_id)
                JOIN player_snapshot AS ps
                  ON ps.ingestion_run_id = r.source_ingestion_run_id
                 AND ps.fpl_id = s.fpl_id
                WHERE s.live_run_id IN ({placeholders}) AND s.player_code IS NOT NULL
                """,
                live_run_ids,
            ).fetchall()
            fixture_cache: dict[tuple[str, int, int], list[datetime]] = {}
            for _, gameweek, snapshot, player_code, minutes, team_id in history_rows:
                key = (str(snapshot), int(gameweek), int(team_id))
                if key not in fixture_cache:
                    fixture_cache[key] = [
                        row[0]
                        for row in connection.execute(
                            """
                            SELECT kickoff_time FROM fixture_snapshot
                            WHERE ingestion_run_id = ? AND gameweek = ?
                              AND (home_team_id = ? OR away_team_id = ?)
                            ORDER BY kickoff_time, fixture_id
                            """,
                            [snapshot, gameweek, team_id, team_id],
                        ).fetchall()
                        if row[0] is not None
                    ]
                if int(minutes) <= 0:
                    continue
                if len(fixture_cache[key]) == 1:
                    appearances.setdefault(int(player_code), []).append(
                        PriorAppearance(fixture_cache[key][0], float(minutes))
                    )
                else:
                    congestion_flags.setdefault(int(player_code), set()).add(
                        "UNSUPPORTED_DGW_CONGESTION_ALLOCATION"
                    )

        output_rows = []
        fully_observed = 0
        for fpl_id, player_code, team_id, current_position in players:
            flags = {
                "CONTEXT_FEATURES_DIAGNOSTIC_ONLY",
                "NON_PL_WORKLOAD_NOT_INGESTED",
                *live_quality_flags,
            }
            manager_name = None
            manager_tenure = None
            manager_changed = None
            manager_rows = by_subject.get(("team", int(team_id), "manager_regime"), [])
            manager = _latest_current(manager_rows, deadline=deadline)
            if manager is None:
                flags.add("MISSING_MANAGER_REGIME_CONTEXT")
            else:
                manager_payload = manager[4]
                manager_name = str(manager_payload["manager_name"])
                regime_start = _parse_date(manager_payload["regime_start"], "regime_start")
                if regime_start > deadline.date():
                    raise ValueError("manager regime_start cannot be after the target deadline")
                manager_tenure = max(0, (deadline.date() - regime_start).days)
                manager_changed = (
                    previous_deadline is not None
                    and regime_start > previous_deadline.date()
                )
                selected_annotation_ids.add(manager[0])
                selected_observed_at.append(manager[1])

            readiness_values: dict[str, float | int | None] | None = None
            readiness_rows = (
                by_subject.get(("player", int(player_code), "readiness"), [])
                if player_code is not None
                else []
            )
            readiness = _latest_current(readiness_rows, deadline=deadline)
            if readiness is None:
                flags.add("MISSING_READINESS_CONTEXT")
            else:
                payload = readiness[4]
                last_tournament_match = (
                    _parse_date(payload["last_tournament_match"], "last_tournament_match")
                    if payload.get("last_tournament_match") is not None
                    else None
                )
                club_return_date = (
                    _parse_date(payload["club_return_date"], "club_return_date")
                    if payload.get("club_return_date") is not None
                    else None
                )
                if any(
                    value is not None and value > deadline.date()
                    for value in (last_tournament_match, club_return_date)
                ):
                    raise ValueError("readiness event dates cannot be after the target deadline")
                readiness_values = TournamentReadiness(
                    tournament_minutes=float(payload.get("tournament_minutes", 0.0)),
                    last_tournament_match=last_tournament_match,
                    club_return_date=club_return_date,
                    preseason_minutes=float(payload.get("preseason_minutes", 0.0)),
                ).features(deadline.date())
                selected_annotation_ids.add(readiness[0])
                selected_observed_at.append(readiness[1])

            role_label = None
            distance = None
            position_changed = None
            role_rows = (
                by_subject.get(("player", int(player_code), "tactical_role"), [])
                if player_code is not None
                else []
            )
            current_role = _latest_current(role_rows, deadline=deadline)
            if current_role is None:
                flags.add("MISSING_TACTICAL_ROLE_CONTEXT")
            else:
                role_label = str(current_role[4]["role_label"])
                earlier = [row for row in role_rows if row[2] < current_role[2]]
                previous_role = max(earlier, key=lambda row: (row[2], row[1], row[0])) if earlier else None
                if previous_role is not None:
                    distance = role_distance(_role(previous_role[4]), _role(current_role[4]))
                    position_changed = str(previous_role[4]["nominal_position"]) != str(
                        current_role[4]["nominal_position"]
                    )
                    selected_annotation_ids.add(previous_role[0])
                    selected_observed_at.append(previous_role[1])
                else:
                    position_changed = str(current_role[4]["nominal_position"]) != str(
                        current_position
                    )
                    flags.add("NO_PRIOR_TACTICAL_ROLE_CONTEXT")
                selected_annotation_ids.add(current_role[0])
                selected_observed_at.append(current_role[1])

            if player_code is None:
                workload = workload_features([], deadline=deadline)
                flags.add("MISSING_PLAYER_CODE")
            else:
                workload = workload_features(
                    appearances.get(int(player_code), []), deadline=deadline
                )
                flags.update(congestion_flags.get(int(player_code), set()))
            complete = manager is not None and readiness is not None and current_role is not None
            if complete and "UNSUPPORTED_DGW_CONGESTION_ALLOCATION" not in flags:
                fully_observed += 1
            output_rows.append(
                (
                    fpl_id,
                    player_code,
                    manager_name,
                    manager_tenure,
                    manager_changed,
                    readiness_values["tournament_minutes"] if readiness_values else None,
                    readiness_values["days_since_last_tournament_match"] if readiness_values else None,
                    readiness_values["training_days"] if readiness_values else None,
                    readiness_values["preseason_minutes"] if readiness_values else None,
                    workload["rest_days"],
                    workload["minutes_last_7d"],
                    workload["minutes_last_14d"],
                    workload["matches_last_7d"],
                    workload["matches_last_14d"],
                    role_label,
                    distance,
                    position_changed,
                    json.dumps(sorted(flags)),
                )
            )

        as_of = max(selected_observed_at)
        if as_of > deadline:
            raise ValueError("context evidence was captured after the target deadline")
        identity = json.dumps(
            {
                "appearance_run": appearance_run,
                "annotation_ids": sorted(selected_annotation_ids),
                "policy_version": POLICY_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        context_run_id = f"context_run_{hashlib.sha256(identity).hexdigest()[:16]}"
        status = "completed" if fully_observed == len(players) else "completed_with_gaps"
        existing = connection.execute(
            "SELECT player_rows, fully_observed_rows, status FROM context_feature_run WHERE context_run_id = ?",
            [context_run_id],
        ).fetchone()
        if existing is not None:
            return ContextFeatureRunResult(
                context_run_id,
                str(appearance_run),
                target_gameweek,
                int(existing[0]),
                int(existing[1]),
                str(existing[2]),
            )
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT INTO context_feature_run VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp
                )
                """,
                [
                    context_run_id,
                    source_run,
                    appearance_run,
                    target_gameweek,
                    as_of,
                    deadline,
                    POLICY_VERSION,
                    len(players),
                    fully_observed,
                    status,
                ],
            )
            connection.executemany(
                "INSERT INTO player_context_feature VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(context_run_id, *row) for row in output_rows],
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")
    return ContextFeatureRunResult(
        context_run_id,
        str(appearance_run),
        target_gameweek,
        len(players),
        fully_observed,
        status,
    )
