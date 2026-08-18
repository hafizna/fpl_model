"""Deterministic triage of preseason player-rate coverage gaps."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pandas as pd

from fpl_model.storage import DEFAULT_DATABASE_PATH

MISSING_RATE_FLAG = "NO_PREVIOUS_PL_PLAYER_RATE_HISTORY"
UNUSABLE_RATE_FLAG = "NO_USABLE_PLAYER_RATE_HISTORY"


def preseason_rate_gap_triage(
    *,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    model_run_id: str | None = None,
    target_gameweek: int = 1,
) -> pd.DataFrame:
    """Return rate gaps ordered by existing expected-minutes evidence.

    The ordering is operational research triage, not a model feature. It does not
    alter xPts or invent a performance prior.
    """
    with duckdb.connect(str(database_path), read_only=True) as connection:
        if model_run_id is None:
            selected = connection.execute(
                """
                SELECT b.model_run_id
                FROM baseline_projection_run AS b
                JOIN model_run AS m USING (model_run_id)
                WHERE m.target_gameweek = ?
                ORDER BY m.created_at DESC, b.model_run_id DESC
                LIMIT 1
                """,
                [target_gameweek],
            ).fetchone()
            if selected is None:
                raise ValueError(
                    f"no baseline projection run found for GW{target_gameweek}"
                )
            model_run_id = selected[0]

        run_exists = connection.execute(
            "SELECT count(*) FROM baseline_projection_run WHERE model_run_id = ?",
            [model_run_id],
        ).fetchone()[0]
        if not run_exists:
            raise ValueError(f"unknown baseline projection run: {model_run_id}")

        frame = connection.execute(
            """
            SELECT g.model_run_id, m.target_gameweek, m.as_of, m.deadline,
                   g.fpl_id, g.player_code, p.web_name AS player_name,
                   t.short_name AS team, p.fpl_position AS position,
                   p.price, p.fpl_status, ps.can_select,
                   ps.selected_by_percent, ap.availability_probability,
                   ap.start_probability, ap.substitute_appearance_probability,
                   ap.expected_minutes, g.data_quality_flags
            FROM baseline_projection_gap AS g
            JOIN baseline_projection_run AS b USING (model_run_id)
            JOIN model_run AS m USING (model_run_id)
            JOIN player_snapshot AS p
              ON p.ingestion_run_id = m.source_ingestion_run_id
             AND p.fpl_id = g.fpl_id
            JOIN team_snapshot AS t
              ON t.ingestion_run_id = m.source_ingestion_run_id
             AND t.team_id = g.team_id
            LEFT JOIN player_status_snapshot AS ps
              ON ps.ingestion_run_id = m.source_ingestion_run_id
             AND ps.fpl_id = g.fpl_id
            LEFT JOIN player_appearance_projection AS ap
              ON ap.projection_run_id = b.appearance_projection_run_id
             AND ap.fpl_id = g.fpl_id
            WHERE g.model_run_id = ?
            """,
            [model_run_id],
        ).fetchdf()

    if frame.empty:
        return frame.assign(rate_history_status=pd.Series(dtype="object"))

    parsed_flags = frame["data_quality_flags"].map(json.loads)
    rate_status = parsed_flags.map(
        lambda flags: (
            "zero_minute_placeholder"
            if UNUSABLE_RATE_FLAG in flags
            else "missing_rate_row"
            if MISSING_RATE_FLAG in flags
            else None
        )
    )
    frame.insert(len(frame.columns) - 1, "rate_history_status", rate_status)
    frame = frame[frame["rate_history_status"].notna()].copy()

    frame["_selectable_sort"] = frame["can_select"].fillna(False).astype(int)
    frame["_minutes_sort"] = frame["expected_minutes"].fillna(-1.0)
    frame["_ownership_sort"] = frame["selected_by_percent"].fillna(-1.0)
    frame = frame.sort_values(
        ["_selectable_sort", "_minutes_sort", "_ownership_sort", "player_name"],
        ascending=[False, False, False, True],
        kind="stable",
    ).drop(columns=["_selectable_sort", "_minutes_sort", "_ownership_sort"])
    frame.insert(0, "research_rank", range(1, len(frame) + 1))
    return frame.reset_index(drop=True)


def export_preseason_rate_gap_triage(
    output_path: str | Path,
    *,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    model_run_id: str | None = None,
    target_gameweek: int = 1,
) -> pd.DataFrame:
    """Write the complete ordered gap allowlist as UTF-8 CSV."""
    frame = preseason_rate_gap_triage(
        database_path=database_path,
        model_run_id=model_run_id,
        target_gameweek=target_gameweek,
    )
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8")
    return frame
