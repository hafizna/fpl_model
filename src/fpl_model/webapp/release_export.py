"""Export one validated projection horizon as a compact web release."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.decision_transparency import (
    load_player_transparency,
    transparency_report,
)
from fpl_model.validation.release_health import determine_release_health
from fpl_model.validation.release_orchestration import orchestrate_release_validation
from fpl_model.webapp.service import (
    ResearchHorizon,
    load_horizon_catalog,
    resolve_research_horizon,
)


@dataclass(frozen=True, slots=True)
class WebReleaseExport:
    payload: dict[str, Any]

    @property
    def release_id(self) -> str:
        return str(self.payload["release"]["release_id"])

    @property
    def health(self) -> str:
        return str(self.payload["release"]["health"])


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _explicit_horizon(
    connection: duckdb.DuckDBPyConnection,
    model_run_ids: tuple[str, ...],
) -> ResearchHorizon:
    if len(model_run_ids) != 3 or len(set(model_run_ids)) != 3:
        raise ValueError("a web release requires exactly three unique model_run_ids")
    placeholders = ",".join("?" for _ in model_run_ids)
    rows = connection.execute(
        f"""
        SELECT target_gameweek, model_run_id, source_ingestion_run_id,
               model_version, as_of
        FROM model_run
        WHERE model_run_id IN ({placeholders}) AND status = 'completed'
        ORDER BY target_gameweek
        """,
        list(model_run_ids),
    ).fetchall()
    if len(rows) != 3:
        raise ValueError("every requested model run must exist and be completed")
    gameweeks = tuple(int(row[0]) for row in rows)
    if gameweeks != tuple(range(gameweeks[0], gameweeks[0] + 3)):
        raise ValueError("model runs must cover three consecutive Gameweeks")
    lineage = {(str(row[2]), str(row[3]), row[4].isoformat()) for row in rows}
    if len(lineage) != 1:
        raise ValueError("model runs do not share one snapshot, version, and planning as_of")
    source_id, version, planning_as_of = lineage.pop()
    return ResearchHorizon(
        source_ingestion_run_id=source_id,
        model_version=version,
        planning_as_of=planning_as_of,
        model_runs=tuple((int(row[0]), str(row[1])) for row in rows),
    )


def build_web_release(
    *,
    model_run_ids: tuple[str, ...] | None = None,
    require_production: bool = False,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> WebReleaseExport:
    """Build a deterministic, manager-agnostic web artifact.

    Manifest and freshness gates are mandatory. ``require_production`` also
    requires approved calibration and uncertainty artifacts; otherwise a
    healthy shadow release remains exportable but is labelled as such.
    """

    with duckdb.connect(str(database_path), read_only=True) as connection:
        horizon = (
            resolve_research_horizon(connection)
            if model_run_ids is None
            else _explicit_horizon(connection, model_run_ids)
        )
        run_ids = tuple(run_id for _, run_id in horizon.model_runs)
        validation = orchestrate_release_validation(
            model_run_ids=run_ids,
            database_path=database_path,
        )
        health = determine_release_health(orchestration_report=validation.report)
        if not validation.passes:
            raise ValueError("release fails manifest or freshness validation")
        if require_production and health.state != "production":
            raise ValueError(
                f"release health is {health.state!r}; production approval is required"
            )

        catalog, projections = load_horizon_catalog(connection, horizon)
        all_fpl_ids = tuple(sorted(catalog))
        for gameweek, model_run_id in horizon.model_runs:
            transparency = load_player_transparency(
                connection,
                model_run_id=model_run_id,
                fpl_ids=all_fpl_ids,
            )
            for fpl_id, player in catalog.items():
                projection = projections[gameweek][fpl_id]
                player["gameweeks"][str(gameweek)].update(
                    {
                        "uncertainty": projection.uncertainty,
                        "quality_flags": list(projection.data_quality_flags),
                        "transparency": transparency_report(transparency.get(fpl_id)),
                    }
                )

    release = {
        "schema_version": "fpl_web_release_v1",
        "release": {
            "health": health.state,
            "label": health.label,
            "source_ingestion_run_id": horizon.source_ingestion_run_id,
            "model_version": horizon.model_version,
            "planning_as_of": horizon.planning_as_of,
            "start_gameweek": horizon.start_gameweek,
            "end_gameweek": horizon.end_gameweek,
            "model_runs": [
                {"gameweek": gameweek, "model_run_id": run_id}
                for gameweek, run_id in horizon.model_runs
            ],
            "validation": {
                "passes": validation.passes,
                "approval_status": validation.approval_status,
                "manifest_id": validation.report["manifest"]["manifest_id"],
            },
        },
        "players": sorted(
            catalog.values(),
            key=lambda row: (row["position"], -row["price_tenths"], row["name"]),
        ),
    }
    digest = hashlib.sha256(_canonical_json(release)).hexdigest()
    release["release"]["release_id"] = f"web_release_{digest[:16]}"
    release["release"]["content_sha256"] = digest
    return WebReleaseExport(payload=release)

