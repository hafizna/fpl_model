"""Materialise and validate one complete in-season three-Gameweek release."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from fpl_model.context.availability import materialize_latest_fpl_availability
from fpl_model.context.pipeline import materialize_context_features
from fpl_model.ingest.fpl import FPLClient
from fpl_model.ingest.fpl_event_live import (
    DEFAULT_RAW_ROOT as DEFAULT_EVENT_LIVE_RAW_ROOT,
)
from fpl_model.ingest.fpl_event_live import persist_fpl_event_live
from fpl_model.ingest.fpl_snapshot import DEFAULT_RAW_ROOT as DEFAULT_SNAPSHOT_RAW_ROOT
from fpl_model.ingest.fpl_snapshot import persist_fpl_snapshot
from fpl_model.ingest.player_identity import import_player_identity_bridge
from fpl_model.ingest.team_strength import (
    import_team_strength_history,
    materialize_preseason_team_strength,
)
from fpl_model.model.appearance_pipeline import materialize_inseason_appearance
from fpl_model.model.baseline_pipeline import (
    materialize_frozen_projection_horizon,
    materialize_inseason_baseline,
)
from fpl_model.model.shadow_calibration import materialize_shadow_calibration
from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.projection_uncertainty import apply_uncertainty_artifact
from fpl_model.validation.release_health import determine_release_health
from fpl_model.validation.release_orchestration import orchestrate_release_validation


class FPLReleaseClient(Protocol):
    def snapshot_payload(self) -> tuple[dict[str, Any], list[dict[str, Any]], datetime]: ...

    def event_live(self, gameweek: int) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ReleaseMaterializationResult:
    report: dict[str, Any]

    @property
    def model_run_ids(self) -> tuple[str, ...]:
        return tuple(self.report["release"]["model_run_ids"])

    @property
    def health(self) -> str:
        return str(self.report["validation"]["health"]["state"])

    @property
    def passes(self) -> bool:
        return bool(self.report["validation"]["passes"])


def materialize_inseason_release(
    *,
    target_gameweek: int,
    current_season: str,
    previous_season: str,
    team_strength_csv: str | Path,
    team_strength_source_label: str,
    vaastav_players_csv: str | Path,
    vaastav_source_revision: str,
    calibration_artifact_id: str,
    uncertainty_artifact_id: str,
    previous_effective_fixtures: float = 5.0,
    allow_analytically_complete: bool = False,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    snapshot_raw_root: str | Path = DEFAULT_SNAPSHOT_RAW_ROOT,
    event_live_raw_root: str | Path = DEFAULT_EVENT_LIVE_RAW_ROOT,
    client: FPLReleaseClient | None = None,
) -> ReleaseMaterializationResult:
    """Fetch, materialise, enrich, and validate one frozen in-season release.

    Every upstream identifier is returned in stage order. Official prior-GW
    evidence is final-only by default. ``allow_analytically_complete`` is an
    explicit research escape hatch and remains visible in event-live status and
    downstream quality flags.
    """

    if not 2 <= target_gameweek <= 36:
        raise ValueError("target_gameweek must be between 2 and 36 for a three-GW horizon")
    if any(
        not value.strip()
        for value in (
            calibration_artifact_id,
            uncertainty_artifact_id,
            team_strength_source_label,
            vaastav_source_revision,
        )
    ):
        raise ValueError("artifact IDs and source labels/revisions are required")
    fpl_client = FPLClient() if client is None else client

    bootstrap, fixtures, snapshot_captured_at = fpl_client.snapshot_payload()
    snapshot = persist_fpl_snapshot(
        bootstrap=bootstrap,
        fixtures=fixtures,
        captured_at=snapshot_captured_at,
        season=current_season,
        database_path=database_path,
        raw_root=snapshot_raw_root,
    )
    identity = import_player_identity_bridge(
        vaastav_players_csv,
        source_ingestion_run_id=snapshot.ingestion_run_id,
        target_season=current_season,
        vaastav_season=previous_season,
        source_revision=vaastav_source_revision,
        database_path=database_path,
    )
    availability = materialize_latest_fpl_availability(
        target_gameweek=target_gameweek,
        database_path=database_path,
    )
    if availability.source_ingestion_run_id != snapshot.ingestion_run_id:
        raise ValueError(
            "new snapshot was not selected as the causal availability source; "
            "its capture may be after the target deadline"
        )

    live_runs = []
    for gameweek in range(1, target_gameweek):
        live_runs.append(
            persist_fpl_event_live(
                payload=fpl_client.event_live(gameweek),
                source_ingestion_run_id=snapshot.ingestion_run_id,
                gameweek=gameweek,
                captured_at=datetime.now(UTC),
                season=current_season,
                require_final=not allow_analytically_complete,
                require_all_fixtures_finished=allow_analytically_complete,
                database_path=database_path,
                raw_root=event_live_raw_root,
            )
        )

    appearance = materialize_inseason_appearance(
        target_gameweek=target_gameweek,
        current_season=current_season,
        previous_season=previous_season,
        availability_resolution_run_id=availability.resolution_run_id,
        previous_effective_fixtures=previous_effective_fixtures,
        database_path=database_path,
    )
    strength_import = import_team_strength_history(
        team_strength_csv,
        target_season=current_season,
        previous_season=previous_season,
        source_label=team_strength_source_label,
        database_path=database_path,
    )
    strength = materialize_preseason_team_strength(
        source_import_run_id=strength_import.import_run_id,
        target_gameweek=target_gameweek,
        source_ingestion_run_id=snapshot.ingestion_run_id,
        database_path=database_path,
    )
    context = materialize_context_features(
        target_gameweek=target_gameweek,
        appearance_projection_run_id=appearance.projection_run_id,
        database_path=database_path,
    )
    anchor = materialize_inseason_baseline(
        target_gameweek=target_gameweek,
        appearance_projection_run_id=appearance.projection_run_id,
        team_strength_run_id=strength.strength_run_id,
        context_feature_run_id=context.context_run_id,
        database_path=database_path,
    )
    horizon = materialize_frozen_projection_horizon(
        anchor_model_run_id=anchor.model_run_id,
        database_path=database_path,
    )

    calibration_rows = []
    uncertainty_rows = []
    for model_run_id in horizon.model_run_ids:
        calibration = materialize_shadow_calibration(
            model_run_id=model_run_id,
            artifact_id=calibration_artifact_id,
            database_path=database_path,
        )
        uncertainty = apply_uncertainty_artifact(
            model_run_id=model_run_id,
            artifact_id=uncertainty_artifact_id,
            database_path=database_path,
        )
        calibration_rows.append(
            {
                "model_run_id": calibration.model_run_id,
                "artifact_id": calibration.artifact_id,
                "player_fixture_rows": calibration.player_fixture_rows,
            }
        )
        uncertainty_rows.append(
            {
                "model_run_id": uncertainty.model_run_id,
                "artifact_id": uncertainty.artifact_id,
                "player_fixture_rows": uncertainty.player_fixture_rows,
                "application_mode": uncertainty.application_mode,
            }
        )

    validation = orchestrate_release_validation(
        model_run_ids=horizon.model_run_ids,
        database_path=database_path,
    )
    health = determine_release_health(orchestration_report=validation.report)
    report = {
        "schema_version": "inseason_release_materialization_v1",
        "inputs": {
            "target_gameweek": target_gameweek,
            "current_season": current_season,
            "previous_season": previous_season,
            "allow_analytically_complete": allow_analytically_complete,
            "previous_effective_fixtures": previous_effective_fixtures,
            "team_strength_csv": str(Path(team_strength_csv)),
            "vaastav_players_csv": str(Path(vaastav_players_csv)),
            "vaastav_source_revision": vaastav_source_revision,
            "calibration_artifact_id": calibration_artifact_id,
            "uncertainty_artifact_id": uncertainty_artifact_id,
        },
        "stages": {
            "snapshot": {
                "ingestion_run_id": snapshot.ingestion_run_id,
                "captured_at": snapshot.captured_at.isoformat(),
                "players": snapshot.players,
                "fixtures": snapshot.fixtures,
            },
            "player_identity": {
                "bridge_run_id": identity.bridge_run_id,
                "status": identity.status,
                "matched_players": identity.matched_players,
                "official_only_players": identity.official_only_players,
                "vaastav_only_players": identity.vaastav_only_players,
                "name_mismatch_players": identity.name_mismatch_players,
            },
            "availability": {
                "resolution_run_id": availability.resolution_run_id,
                "status": availability.status,
            },
            "event_live": [
                {
                    "gameweek": row.gameweek,
                    "live_run_id": row.live_run_id,
                    "status": row.status,
                }
                for row in live_runs
            ],
            "appearance": {
                "projection_run_id": appearance.projection_run_id,
                "status": appearance.status,
            },
            "team_strength": {
                "import_run_id": strength_import.import_run_id,
                "strength_run_id": strength.strength_run_id,
            },
            "context": {
                "context_run_id": context.context_run_id,
                "status": context.status,
            },
            "anchor": {
                "model_run_id": anchor.model_run_id,
                "status": anchor.status,
            },
            "shadow_calibration": calibration_rows,
            "uncertainty": uncertainty_rows,
        },
        "release": {
            "start_gameweek": horizon.start_gameweek,
            "end_gameweek": horizon.end_gameweek,
            "model_run_ids": list(horizon.model_run_ids),
        },
        "validation": {
            **validation.report,
            "health": health.report,
        },
    }
    return ReleaseMaterializationResult(report=report)
