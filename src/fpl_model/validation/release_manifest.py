"""Immutable release manifest linking one Gameweek horizon's full upstream lineage.

Sprint 5 requires one auditable manifest before any decision surface may drop the
`RESEARCH_ONLY` label. This module does not itself decide freshness, coverage,
calibration, or drift pass/fail -- those gates are (or will be) separate, versioned
checks. Its job is narrower and prerequisite to all of them: given the model runs a
manager intends to treat as one release, walk every upstream run they actually
recorded, confirm they cohere as a single horizon (same official snapshot, same
frozen ``as_of``), and produce one reproducible, content-hashed document that later
gates and the decision layer can all point at instead of re-deriving lineage
themselves.

A manifest never guesses "the latest run". Callers pass explicit ``model_run_id``s
so a release is a deliberate, reproducible act, not whatever happened to be most
recent when a script ran.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from fpl_model.storage import DEFAULT_DATABASE_PATH

REQUIRED_STATUSES = {"completed", "completed_with_gaps"}


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    report: dict[str, Any]

    @property
    def manifest_id(self) -> str:
        return str(self.report["manifest_id"])

    @property
    def passes_linkage_gate(self) -> bool:
        return bool(self.report["linkage"]["passes"])


def _row(connection: duckdb.DuckDBPyConnection, sql: str, params: list[Any]) -> tuple | None:
    return connection.execute(sql, params).fetchone()


def _model_run(connection: duckdb.DuckDBPyConnection, model_run_id: str) -> dict[str, Any]:
    row = _row(
        connection,
        """
        SELECT model_run_id, target_gameweek, as_of, deadline, model_version,
               source_ingestion_run_id, status, completed_at
        FROM model_run
        WHERE model_run_id = ?
        """,
        [model_run_id],
    )
    if row is None:
        raise ValueError(f"unknown model_run_id: {model_run_id}")
    (
        run_id,
        gameweek,
        as_of,
        deadline,
        model_version,
        source_ingestion_run_id,
        status,
        completed_at,
    ) = row
    return {
        "model_run_id": str(run_id),
        "target_gameweek": int(gameweek),
        "as_of": as_of.isoformat(),
        "deadline": deadline.isoformat(),
        "model_version": str(model_version),
        "source_ingestion_run_id": str(source_ingestion_run_id),
        "status": str(status),
        "completed_at": completed_at.isoformat() if completed_at is not None else None,
    }


def _baseline_projection_run(
    connection: duckdb.DuckDBPyConnection, model_run_id: str
) -> dict[str, Any] | None:
    row = _row(
        connection,
        """
        SELECT appearance_projection_run_id, player_rate_run_id, team_strength_run_id,
               policy_version, current_players, candidate_fixture_rows,
               projected_fixture_rows, gap_players, status
        FROM baseline_projection_run
        WHERE model_run_id = ?
        """,
        [model_run_id],
    )
    if row is None:
        return None
    (
        appearance_run,
        rate_run,
        strength_run,
        policy_version,
        current_players,
        candidate_rows,
        projected_rows,
        gap_players,
        status,
    ) = row
    return {
        "appearance_projection_run_id": str(appearance_run) if appearance_run else None,
        "player_rate_run_id": str(rate_run) if rate_run else None,
        "team_strength_run_id": str(strength_run) if strength_run else None,
        "policy_version": str(policy_version),
        "current_players": int(current_players),
        "candidate_fixture_rows": int(candidate_rows),
        "projected_fixture_rows": int(projected_rows),
        "gap_players": int(gap_players),
        "status": str(status),
    }


def _ingestion_run(connection: duckdb.DuckDBPyConnection, ingestion_run_id: str) -> dict[str, Any]:
    row = _row(
        connection,
        """
        SELECT source, captured_at, source_as_of, completed_at, status, payload_sha256
        FROM ingestion_run
        WHERE ingestion_run_id = ?
        """,
        [ingestion_run_id],
    )
    if row is None:
        raise ValueError(f"unknown ingestion_run_id: {ingestion_run_id}")
    source, captured_at, source_as_of, completed_at, status, payload_sha256 = row
    return {
        "ingestion_run_id": ingestion_run_id,
        "source": str(source),
        "captured_at": captured_at.isoformat() if captured_at is not None else None,
        "source_as_of": source_as_of.isoformat() if source_as_of is not None else None,
        "completed_at": completed_at.isoformat() if completed_at is not None else None,
        "status": str(status),
        "payload_sha256": payload_sha256,
    }


def _identity_bridge_run(
    connection: duckdb.DuckDBPyConnection, source_ingestion_run_id: str
) -> dict[str, Any] | None:
    row = _row(
        connection,
        """
        SELECT bridge_run_id, official_players, vaastav_players, matched_players,
               official_only_players, vaastav_only_players, status
        FROM player_identity_bridge_run
        WHERE source_ingestion_run_id = ?
        ORDER BY created_at DESC, bridge_run_id DESC
        LIMIT 1
        """,
        [source_ingestion_run_id],
    )
    if row is None:
        return None
    bridge_run_id, official, vaastav, matched, official_only, vaastav_only, status = row
    return {
        "bridge_run_id": str(bridge_run_id),
        "official_players": int(official),
        "vaastav_players": int(vaastav),
        "matched_players": int(matched),
        "official_only_players": int(official_only),
        "vaastav_only_players": int(vaastav_only),
        "status": str(status),
    }


def _event_live_runs(
    connection: duckdb.DuckDBPyConnection, live_run_ids: list[str]
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for live_run_id in live_run_ids:
        row = _row(
            connection,
            """
            SELECT live_run_id, season, gameweek, captured_at, event_finished,
                   data_checked, player_rows, status
            FROM fpl_event_live_run
            WHERE live_run_id = ?
            """,
            [live_run_id],
        )
        if row is None:
            raise ValueError(f"unknown fpl_event_live_run live_run_id: {live_run_id}")
        run_id, season, gameweek, captured_at, event_finished, data_checked, player_rows, status = row
        resolved.append(
            {
                "live_run_id": str(run_id),
                "season": str(season),
                "gameweek": int(gameweek),
                "captured_at": captured_at.isoformat() if captured_at is not None else None,
                "event_finished": bool(event_finished),
                "data_checked": bool(data_checked),
                "player_rows": int(player_rows),
                "status": str(status),
            }
        )
    return resolved


def _appearance_lineage(
    connection: duckdb.DuckDBPyConnection, appearance_projection_run_id: str
) -> dict[str, Any]:
    # An in-season run inserts into BOTH appearance_projection_run (shared PK,
    # status, policy_version) and inseason_appearance_run (the in-season-specific
    # fields) -- see model/appearance_pipeline.py. inseason_appearance_run's
    # presence is therefore the distinguishing signal and must be checked first;
    # a plain appearance_projection_run row with no inseason_appearance_run match
    # is a genuine preseason run.
    inseason = _row(
        connection,
        """
        SELECT current_season, previous_season, first_history_gameweek,
               last_history_gameweek, live_run_ids, policy_version
        FROM inseason_appearance_run
        WHERE projection_run_id = ?
        """,
        [appearance_projection_run_id],
    )
    if inseason is not None:
        current_season, previous_season, first_gw, last_gw, live_run_ids_raw, policy_version = inseason
        live_run_ids = json.loads(live_run_ids_raw) if live_run_ids_raw else []
        return {
            "kind": "inseason",
            "projection_run_id": appearance_projection_run_id,
            "current_season": str(current_season),
            "previous_season": str(previous_season),
            "first_history_gameweek": int(first_gw),
            "last_history_gameweek": int(last_gw),
            "event_live_runs": _event_live_runs(connection, live_run_ids),
            "policy_version": str(policy_version),
            "status": None,
        }
    preseason = _row(
        connection,
        """
        SELECT availability_resolution_run_id, appearance_history_import_run_id,
               target_gameweek, policy_version, status
        FROM appearance_projection_run
        WHERE projection_run_id = ?
        """,
        [appearance_projection_run_id],
    )
    if preseason is None:
        raise ValueError(
            f"appearance_projection_run_id {appearance_projection_run_id} not found in "
            "either preseason or in-season appearance run tables"
        )
    resolution_run, history_run, gameweek, policy_version, status = preseason
    return {
        "kind": "preseason",
        "projection_run_id": appearance_projection_run_id,
        "availability_resolution_run_id": str(resolution_run) if resolution_run else None,
        "appearance_history_import_run_id": str(history_run) if history_run else None,
        "target_gameweek": int(gameweek),
        "policy_version": str(policy_version),
        "status": str(status),
    }


def _player_rate_run(connection: duckdb.DuckDBPyConnection, rate_run_id: str) -> dict[str, Any]:
    row = _row(
        connection,
        """
        SELECT source_import_run_id, policy_version, player_rows, status
        FROM player_rate_history_run
        WHERE rate_run_id = ?
        """,
        [rate_run_id],
    )
    if row is None:
        raise ValueError(f"unknown player_rate_run_id: {rate_run_id}")
    source_import_run, policy_version, player_rows, status = row
    return {
        "rate_run_id": rate_run_id,
        "source_import_run_id": str(source_import_run) if source_import_run else None,
        "policy_version": str(policy_version),
        "player_rows": int(player_rows),
        "status": str(status),
    }


def _team_strength_run(
    connection: duckdb.DuckDBPyConnection, strength_run_id: str
) -> dict[str, Any]:
    row = _row(
        connection,
        """
        SELECT source_import_run_id, source_ingestion_run_id, target_gameweek,
               policy_version, team_rows, status
        FROM team_strength_run
        WHERE strength_run_id = ?
        """,
        [strength_run_id],
    )
    if row is None:
        raise ValueError(f"unknown team_strength_run_id: {strength_run_id}")
    source_import_run, source_ingestion_run, gameweek, policy_version, team_rows, status = row
    return {
        "strength_run_id": strength_run_id,
        "source_import_run_id": str(source_import_run) if source_import_run else None,
        "source_ingestion_run_id": str(source_ingestion_run) if source_ingestion_run else None,
        "target_gameweek": int(gameweek),
        "policy_version": str(policy_version),
        "team_rows": int(team_rows),
        "status": str(status),
    }


def _context_run(connection: duckdb.DuckDBPyConnection, model_run_id: str) -> dict[str, Any] | None:
    row = _row(
        connection,
        """
        SELECT c.context_run_id, c.target_gameweek, c.policy_version,
               c.player_rows, c.fully_observed_rows, c.status
        FROM baseline_context_lineage AS l
        JOIN context_feature_run AS c USING (context_run_id)
        WHERE l.model_run_id = ?
        """,
        [model_run_id],
    )
    if row is None:
        return None
    context_run_id, gameweek, policy_version, player_rows, fully_observed, status = row
    return {
        "context_run_id": str(context_run_id),
        "target_gameweek": int(gameweek),
        "policy_version": str(policy_version),
        "player_rows": int(player_rows),
        "fully_observed_rows": int(fully_observed),
        "status": str(status),
        "note": "context_adjustment remains zero; diagnostic-only per docs/INSEASON_REFRESH.md",
    }


def _shadow_calibration(
    connection: duckdb.DuckDBPyConnection, model_run_id: str
) -> dict[str, Any] | None:
    row = _row(
        connection,
        """
        SELECT a.artifact_id, a.calibration_type, a.source_season,
               a.source_model_version, a.policy_version, a.slope, a.intercept, a.status
        FROM model_shadow_calibration_lineage AS l
        JOIN shadow_calibration_artifact AS a USING (artifact_id)
        WHERE l.model_run_id = ?
        """,
        [model_run_id],
    )
    if row is None:
        return None
    artifact_id, calibration_type, source_season, source_model_version, policy_version, slope, intercept, status = row
    return {
        "artifact_id": str(artifact_id),
        "calibration_type": str(calibration_type),
        "source_season": str(source_season),
        "source_model_version": str(source_model_version),
        "policy_version": str(policy_version),
        "slope": float(slope),
        "intercept": float(intercept),
        "status": str(status),
    }


def _uncertainty(connection: duckdb.DuckDBPyConnection, model_run_id: str) -> dict[str, Any] | None:
    row = _row(
        connection,
        """
        SELECT a.artifact_id, a.source_season, a.source_model_version,
               a.policy_version, a.interval_mass, a.segment_rows, a.status,
               l.application_mode
        FROM model_uncertainty_lineage AS l
        JOIN uncertainty_artifact AS a USING (artifact_id)
        WHERE l.model_run_id = ?
        """,
        [model_run_id],
    )
    if row is None:
        return None
    artifact_id, source_season, source_model_version, policy_version, interval_mass, segment_rows, status, application_mode = row
    return {
        "artifact_id": str(artifact_id),
        "source_season": str(source_season),
        "source_model_version": str(source_model_version),
        "policy_version": str(policy_version),
        "interval_mass": float(interval_mass),
        "segment_rows": int(segment_rows),
        "status": str(status),
        "application_mode": str(application_mode),
    }


def _gameweek_entry(connection: duckdb.DuckDBPyConnection, model_run_id: str) -> dict[str, Any]:
    model_run = _model_run(connection, model_run_id)
    baseline = _baseline_projection_run(connection, model_run_id)

    appearance_lineage = None
    player_rate_run = None
    team_strength = None
    if baseline is not None:
        if baseline["appearance_projection_run_id"] is not None:
            appearance_lineage = _appearance_lineage(
                connection, baseline["appearance_projection_run_id"]
            )
        if baseline["player_rate_run_id"] is not None:
            player_rate_run = _player_rate_run(connection, baseline["player_rate_run_id"])
        if baseline["team_strength_run_id"] is not None:
            team_strength = _team_strength_run(connection, baseline["team_strength_run_id"])

    return {
        "model_run": model_run,
        "baseline_projection_run": baseline,
        "ingestion_run": _ingestion_run(connection, model_run["source_ingestion_run_id"]),
        "player_identity_bridge_run": _identity_bridge_run(
            connection, model_run["source_ingestion_run_id"]
        ),
        "appearance_lineage": appearance_lineage,
        "player_rate_run": player_rate_run,
        "team_strength_run": team_strength,
        "context_run": _context_run(connection, model_run_id),
        "shadow_calibration": _shadow_calibration(connection, model_run_id),
        "uncertainty": _uncertainty(connection, model_run_id),
    }


def _content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_release_manifest(
    *,
    model_run_ids: tuple[str, ...],
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> ReleaseManifest:
    """Link one horizon's model runs into one immutable, content-hashed manifest.

    ``model_run_ids`` must be given explicitly, ordered by target Gameweek, and
    normally correspond to one anchor GW plus its frozen GW+1/GW+2 horizon (see
    ``docs/PIPELINE_ARCHITECTURE.md``). This function never selects "the latest"
    run on its own.
    """
    if not model_run_ids:
        raise ValueError("build_release_manifest requires at least one model_run_id")
    if len(set(model_run_ids)) != len(model_run_ids):
        raise ValueError("model_run_ids contains duplicates")

    with duckdb.connect(str(database_path), read_only=True) as connection:
        gameweeks = [_gameweek_entry(connection, run_id) for run_id in model_run_ids]

    problems: list[str] = []

    statuses = {gw["model_run"]["status"] for gw in gameweeks}
    if not statuses <= REQUIRED_STATUSES:
        problems.append(
            f"every model_run must be completed or completed_with_gaps; found statuses={sorted(statuses)}"
        )

    target_gameweeks = [gw["model_run"]["target_gameweek"] for gw in gameweeks]
    if len(set(target_gameweeks)) != len(target_gameweeks):
        problems.append(f"model_run_ids must target distinct gameweeks; found {target_gameweeks}")
    if target_gameweeks != sorted(target_gameweeks):
        problems.append(f"model_run_ids must be ordered by ascending target_gameweek; got {target_gameweeks}")

    source_ingestion_runs = {gw["model_run"]["source_ingestion_run_id"] for gw in gameweeks}
    if len(source_ingestion_runs) > 1:
        problems.append(
            "model runs do not share one official snapshot: "
            f"source_ingestion_run_id values={sorted(source_ingestion_runs)}"
        )

    as_of_values = {gw["model_run"]["as_of"] for gw in gameweeks}
    if len(as_of_values) > 1:
        problems.append(
            f"model runs do not share one frozen as_of: as_of values={sorted(as_of_values)}"
        )

    for gw in gameweeks:
        gameweek = gw["model_run"]["target_gameweek"]
        if gw["baseline_projection_run"] is None:
            problems.append(f"GW{gameweek}: no baseline_projection_run linked to its model_run")
            continue
        if gw["player_identity_bridge_run"] is None:
            problems.append(f"GW{gameweek}: no player identity bridge for its source snapshot")
        if gw["appearance_lineage"] is None:
            problems.append(f"GW{gameweek}: baseline_projection_run has no appearance lineage")
        if gw["player_rate_run"] is None:
            problems.append(f"GW{gameweek}: baseline_projection_run has no player-rate lineage")
        if gw["team_strength_run"] is None:
            problems.append(f"GW{gameweek}: baseline_projection_run has no team-strength lineage")

    missing_shadow_calibration = [
        gw["model_run"]["target_gameweek"] for gw in gameweeks if gw["shadow_calibration"] is None
    ]
    missing_uncertainty = [
        gw["model_run"]["target_gameweek"] for gw in gameweeks if gw["uncertainty"] is None
    ]
    missing_context = [
        gw["model_run"]["target_gameweek"] for gw in gameweeks if gw["context_run"] is None
    ]
    non_final_event_live: list[dict[str, Any]] = []
    for gw in gameweeks:
        appearance = gw["appearance_lineage"]
        if appearance is None or appearance.get("kind") != "inseason":
            continue
        for live_run in appearance["event_live_runs"]:
            if not (live_run["event_finished"] and live_run["data_checked"]):
                non_final_event_live.append(
                    {
                        "target_gameweek": gw["model_run"]["target_gameweek"],
                        "live_run_id": live_run["live_run_id"],
                        "event_gameweek": live_run["gameweek"],
                        "event_finished": live_run["event_finished"],
                        "data_checked": live_run["data_checked"],
                    }
                )

    passes_linkage = not problems
    payload: dict[str, Any] = {
        "label": "release_manifest_v1",
        "model_run_ids": list(model_run_ids),
        "gameweeks": gameweeks,
        "linkage": {
            "passes": passes_linkage,
            "problems": problems,
        },
        "shadow_status": {
            "shadow_calibration_missing_for_gameweeks": missing_shadow_calibration,
            "uncertainty_missing_for_gameweeks": missing_uncertainty,
            "context_missing_for_gameweeks": missing_context,
            "non_final_event_live_runs": non_final_event_live,
            "note": (
                "Shadow calibration/uncertainty/context artifacts are measurement-only "
                "per docs/SPRINT4_UNCERTAINTY_AND_CALIBRATION.md and docs/INSEASON_REFRESH.md. "
                "Their absence here does not fail linkage; it is a visibility signal for the "
                "separate Sprint 5 calibration/uncertainty release gates. "
                "non_final_event_live_runs lists in-season appearance inputs built from an "
                "OFFICIAL_EVENT_ANALYTICALLY_COMPLETE_NOT_FINAL run per docs/INSEASON_REFRESH.md; "
                "it does not fail linkage either -- finality is a separate future gate."
            ),
        },
        "limitations": [
            "This manifest links and validates lineage coherence only. It does not itself "
            "check freshness, FPL finality, coverage, calibration, uncertainty, or "
            "provisional-to-final drift -- those are separate Sprint 5 gates.",
            "Passing linkage does not mean the release may drop RESEARCH_ONLY; every other "
            "Sprint 5 gate must also pass.",
            "model_run_ids are supplied by the caller and never auto-selected as 'latest'.",
        ],
    }
    manifest_id = f"release_manifest_{_content_hash(payload)[:16]}"
    payload = {"manifest_id": manifest_id, **payload}
    return ReleaseManifest(report=payload)
