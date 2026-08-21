"""Deadline-pinned classification of baseline projection coverage gaps."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from fpl_model.storage import DEFAULT_DATABASE_PATH

FLAG_ROSTER_BLOCKED = "FPL_ROSTER_BLOCKED"
FLAG_MISSING_APPEARANCE = "MISSING_APPEARANCE_PROJECTION"
FLAG_MISSING_RATE = "NO_PREVIOUS_PL_PLAYER_RATE_HISTORY"
FLAG_UNUSABLE_RATE = "NO_USABLE_PLAYER_RATE_HISTORY"
FLAG_PROMOTED = "OWN_TEAM_PROMOTED_PRIOR"

CHEAP_ENABLER_MAX_PRICE = {
    "GK": 4.5,
    "DEF": 4.5,
    "MID": 5.5,
    "FWD": 5.5,
}


@dataclass(frozen=True, slots=True)
class ProjectionCoverageAudit:
    report: dict[str, Any]
    gaps: pd.DataFrame


def _parse_flags(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        parsed = json.loads(value)
    elif isinstance(value, (list, tuple)):
        parsed = value
    else:
        raise ValueError("data_quality_flags must be a JSON array or sequence")
    if not isinstance(parsed, (list, tuple)) or not all(
        isinstance(item, str) and item for item in parsed
    ):
        raise ValueError("data_quality_flags must contain non-blank strings")
    return tuple(sorted(set(parsed)))


def _primary_reason(flags: set[str], official_only: bool) -> str:
    if FLAG_ROSTER_BLOCKED in flags:
        return "roster_blocked"
    missing_appearance = FLAG_MISSING_APPEARANCE in flags
    missing_or_unusable_rate = bool(
        {FLAG_MISSING_RATE, FLAG_UNUSABLE_RATE} & flags
    )
    if missing_appearance and missing_or_unusable_rate:
        return "missing_appearance_and_rate"
    if missing_appearance:
        return "missing_appearance_only"
    if FLAG_UNUSABLE_RATE in flags:
        return "unusable_previous_pl_rate"
    if FLAG_MISSING_RATE in flags and FLAG_PROMOTED in flags:
        return "promoted_no_previous_pl_rate"
    if FLAG_MISSING_RATE in flags and official_only:
        return "current_only_no_previous_pl_rate"
    if FLAG_MISSING_RATE in flags:
        return "unclassified_missing_previous_pl_rate"
    return "other_projection_gap"


def classify_projection_gaps(frame: pd.DataFrame) -> pd.DataFrame:
    """Add mutually exclusive root causes and orthogonal research cohorts."""
    required = {
        "fpl_id",
        "player_code",
        "player_name",
        "team",
        "position",
        "price",
        "can_select",
        "selected_by_percent",
        "expected_minutes",
        "official_only",
        "data_quality_flags",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError("projection gaps missing columns: " + ", ".join(sorted(missing)))
    result = frame.copy()
    parsed = result["data_quality_flags"].map(_parse_flags)
    result["data_quality_flags"] = parsed

    primary_reasons: list[str] = []
    identity_cohorts: list[str] = []
    rate_statuses: list[str] = []
    appearance_statuses: list[str] = []
    roster_statuses: list[str] = []
    cheap_enablers: list[bool] = []
    for row, flags_tuple in zip(result.itertuples(index=False), parsed, strict=True):
        flags = set(flags_tuple)
        official_only = bool(row.official_only)
        promoted = FLAG_PROMOTED in flags
        primary_reasons.append(_primary_reason(flags, official_only))
        identity_cohorts.append(
            "current_only_promoted"
            if official_only and promoted
            else "current_only_non_promoted"
            if official_only
            else "previous_pl_linked_promoted"
            if promoted
            else "previous_pl_linked"
        )
        rate_statuses.append(
            "missing_previous_pl_rate"
            if FLAG_MISSING_RATE in flags
            else "unusable_previous_pl_rate"
            if FLAG_UNUSABLE_RATE in flags
            else "usable_rate"
        )
        appearance_statuses.append(
            "missing_appearance"
            if FLAG_MISSING_APPEARANCE in flags
            else "appearance_available"
        )
        roster_statuses.append(
            "blocked" if FLAG_ROSTER_BLOCKED in flags else "selectable"
        )
        max_price = CHEAP_ENABLER_MAX_PRICE.get(str(row.position))
        cheap_enablers.append(
            max_price is not None and float(row.price) <= max_price
        )

    result.insert(0, "primary_reason", primary_reasons)
    result.insert(1, "identity_cohort", identity_cohorts)
    result.insert(2, "rate_status", rate_statuses)
    result.insert(3, "appearance_status", appearance_statuses)
    result.insert(4, "roster_status", roster_statuses)
    result.insert(5, "cheap_enabler", cheap_enablers)
    result["_selectable_sort"] = result["roster_status"].eq("selectable").astype(int)
    result["_minutes_sort"] = result["expected_minutes"].fillna(-1.0)
    result["_ownership_sort"] = result["selected_by_percent"].fillna(-1.0)
    result = result.sort_values(
        [
            "_selectable_sort",
            "cheap_enabler",
            "_minutes_sort",
            "_ownership_sort",
            "player_name",
        ],
        ascending=[False, False, False, False, True],
        kind="stable",
    ).drop(columns=["_selectable_sort", "_minutes_sort", "_ownership_sort"])
    result.insert(0, "research_priority", range(1, len(result) + 1))
    return result.reset_index(drop=True)


def _summary(frame: pd.DataFrame, column: str) -> list[dict[str, object]]:
    counts = frame[column].value_counts(dropna=False)
    return [
        {column: None if pd.isna(value) else value, "players": int(count)}
        for value, count in counts.items()
    ]


def _json_row(row: pd.Series) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in row.items():
        if isinstance(value, tuple):
            output[key] = list(value)
        elif pd.isna(value):
            output[key] = None
        elif hasattr(value, "item"):
            output[key] = value.item()
        else:
            output[key] = value
    return output


def audit_projection_coverage(
    *,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    model_run_id: str | None = None,
    target_gameweek: int = 1,
) -> ProjectionCoverageAudit:
    """Audit one immutable baseline run and classify every stored gap."""
    with duckdb.connect(str(database_path), read_only=True) as connection:
        if model_run_id is None:
            selected = connection.execute(
                """
                SELECT b.model_run_id
                FROM baseline_projection_run AS b
                JOIN model_run AS m USING (model_run_id)
                WHERE m.target_gameweek = ? AND m.status = 'completed'
                ORDER BY m.completed_at DESC, b.model_run_id DESC
                LIMIT 1
                """,
                [target_gameweek],
            ).fetchone()
            if selected is None:
                raise ValueError(f"no completed baseline run found for GW{target_gameweek}")
            model_run_id = str(selected[0])

        metadata = connection.execute(
            """
            SELECT m.source_ingestion_run_id, m.target_gameweek, m.as_of,
                   m.deadline, m.model_version, b.current_players,
                   b.projected_fixture_rows, b.gap_players,
                   b.appearance_projection_run_id, b.player_rate_run_id,
                   b.team_strength_run_id
            FROM baseline_projection_run AS b
            JOIN model_run AS m USING (model_run_id)
            WHERE b.model_run_id = ?
            """,
            [model_run_id],
        ).fetchone()
        if metadata is None:
            raise ValueError(f"unknown baseline model run: {model_run_id}")
        (
            source_run,
            gameweek,
            as_of,
            deadline,
            model_version,
            snapshot_players,
            projected_rows,
            gap_players,
            appearance_run,
            rate_run,
            strength_run,
        ) = metadata

        bridge = connection.execute(
            """
            SELECT bridge_run_id FROM player_identity_bridge_run
            WHERE source_ingestion_run_id = ?
            ORDER BY created_at DESC, bridge_run_id DESC LIMIT 1
            """,
            [source_run],
        ).fetchone()
        if bridge is None:
            raise ValueError(
                "projection coverage audit requires a player identity bridge for "
                f"source snapshot {source_run}"
            )
        bridge_run_id = str(bridge[0])

        gaps = connection.execute(
            """
            SELECT g.fpl_id, g.player_code, p.web_name AS player_name,
                   t.short_name AS team, p.fpl_position AS position, p.price,
                   p.fpl_status, ps.can_select, ps.can_transact,
                   ps.selected_by_percent, ap.availability_probability,
                   ap.start_probability, ap.expected_minutes,
                   contains(ib.data_quality_flags, 'MISSING_VAASTAV_ID')
                       AS official_only,
                   g.data_quality_flags
            FROM baseline_projection_gap AS g
            JOIN baseline_projection_run AS b USING (model_run_id)
            JOIN model_run AS m USING (model_run_id)
            JOIN player_snapshot AS p
              ON p.ingestion_run_id = m.source_ingestion_run_id
             AND p.fpl_id = g.fpl_id
            JOIN team_snapshot AS t
              ON t.ingestion_run_id = m.source_ingestion_run_id
             AND t.team_id = p.team_id
            LEFT JOIN player_status_snapshot AS ps
              ON ps.ingestion_run_id = m.source_ingestion_run_id
             AND ps.fpl_id = g.fpl_id
            LEFT JOIN player_appearance_projection AS ap
              ON ap.projection_run_id = b.appearance_projection_run_id
             AND ap.fpl_id = g.fpl_id
            LEFT JOIN player_identity_bridge AS ib
              ON ib.bridge_run_id = ?
             AND ib.provider = 'official_fpl'
             AND ib.provider_player_id = cast(g.fpl_id AS VARCHAR)
            WHERE g.model_run_id = ?
            ORDER BY g.fpl_id
            """,
            [bridge_run_id, model_run_id],
        ).fetchdf()
        if len(gaps) != int(gap_players):
            raise ValueError(
                f"stored gap count mismatch: metadata={gap_players}, rows={len(gaps)}"
            )
        if gaps["official_only"].isna().any():
            raise ValueError("identity bridge does not cover every projection gap")

        selectable_players = int(
            connection.execute(
                """
                SELECT count(*)
                FROM player_snapshot AS p
                JOIN player_status_snapshot AS ps
                  ON ps.ingestion_run_id = p.ingestion_run_id
                 AND ps.fpl_id = p.fpl_id
                WHERE p.ingestion_run_id = ? AND ps.can_select
                """,
                [source_run],
            ).fetchone()[0]
        )
        selectable_gap_players = int(gaps["can_select"].fillna(False).sum())
        selectable_projected_players = selectable_players - selectable_gap_players

    classified = classify_projection_gaps(gaps)
    selectable_coverage = (
        selectable_projected_players / selectable_players
        if selectable_players
        else 0.0
    )
    report: dict[str, Any] = {
        "label": "projection_coverage_gap_audit_v1",
        "model_run_id": model_run_id,
        "model_version": str(model_version),
        "source_ingestion_run_id": str(source_run),
        "player_identity_bridge_run_id": bridge_run_id,
        "target_gameweek": int(gameweek),
        "as_of": as_of.isoformat(),
        "deadline": deadline.isoformat(),
        "lineage": {
            "appearance_projection_run_id": str(appearance_run),
            "player_rate_run_id": str(rate_run),
            "team_strength_run_id": str(strength_run),
        },
        "coverage": {
            "snapshot_players": int(snapshot_players),
            "projected_fixture_rows": int(projected_rows),
            "gap_players": int(gap_players),
            "selectable_players": selectable_players,
            "selectable_projected_players": selectable_projected_players,
            "selectable_gap_players": selectable_gap_players,
            "selectable_coverage": selectable_coverage,
            "target_selectable_coverage": 0.95,
            "passes_target": selectable_coverage >= 0.95,
        },
        "summaries": {
            "primary_reason": _summary(classified, "primary_reason"),
            "identity_cohort": _summary(classified, "identity_cohort"),
            "rate_status": _summary(classified, "rate_status"),
            "appearance_status": _summary(classified, "appearance_status"),
            "roster_status": _summary(classified, "roster_status"),
            "position": _summary(classified, "position"),
            "cheap_enabler": _summary(classified, "cheap_enabler"),
        },
        "classification_precedence": [
            "roster_blocked",
            "missing_appearance_and_rate",
            "missing_appearance_only",
            "unusable_previous_pl_rate",
            "promoted_no_previous_pl_rate",
            "current_only_no_previous_pl_rate",
            "unclassified_missing_previous_pl_rate",
            "other_projection_gap",
        ],
        "limitations": [
            "This audit classifies existing gaps; it does not create or apply performance priors.",
            "Current-only identifies absence from the pinned previous-season Vaastav player list, not the player's transfer origin.",
            "Cheap-enabler thresholds are research cohorts, not hard optimizer constraints.",
        ],
        "gaps": [_json_row(row) for _, row in classified.iterrows()],
    }
    return ProjectionCoverageAudit(report=report, gaps=classified)
