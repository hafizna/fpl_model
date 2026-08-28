"""Platform-neutral, fail-closed deadline refresh worker."""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from fpl_model.validation.release_materialization import (
    ReleaseMaterializationResult,
    materialize_inseason_release,
)
from fpl_model.webapp.release_export import WebReleaseExport, build_web_release


@dataclass(frozen=True, slots=True)
class DeadlineRefreshConfig:
    target_gameweek: int
    current_season: str
    previous_season: str
    team_strength_csv: Path
    team_strength_source_label: str
    vaastav_players_csv: Path
    vaastav_source_revision: str
    calibration_artifact_id: str
    uncertainty_artifact_id: str
    database_path: Path
    web_release_output: Path
    status_output: Path
    materialization_report_output: Path
    lock_file: Path
    backup_directory: Path | None = None
    previous_effective_fixtures: float = 5.0
    allow_analytically_complete: bool = False
    require_production: bool = False
    alert_webhook_url: str | None = field(default=None, repr=False)
    alert_timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not 2 <= self.target_gameweek <= 36:
            raise ValueError("target_gameweek must be between 2 and 36")
        if self.previous_effective_fixtures <= 0:
            raise ValueError("previous_effective_fixtures must be positive")
        if self.alert_timeout_seconds <= 0:
            raise ValueError("alert_timeout_seconds must be positive")
        required_labels = (
            self.current_season,
            self.previous_season,
            self.team_strength_source_label,
            self.vaastav_source_revision,
            self.calibration_artifact_id,
            self.uncertainty_artifact_id,
        )
        if any(not value.strip() for value in required_labels):
            raise ValueError("season, source, revision, and artifact values must be non-empty")


@dataclass(frozen=True, slots=True)
class DeadlineRefreshResult:
    report: dict[str, Any]

    @property
    def succeeded(self) -> bool:
        return self.report["status"] == "succeeded"

    @property
    def exit_code(self) -> int:
        if not self.succeeded:
            return 2
        if self.report["alert"]["status"] == "failed":
            return 3
        return 0


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
            temporary_path = Path(out.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _existing_release_id(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return str(payload["release"]["release_id"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


@contextmanager
def _exclusive_lock(path: Path, *, started_at: datetime):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(
            f"refresh lock already exists at {path}; inspect the running worker before removing it"
        ) from exc
    try:
        lock_payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": started_at.isoformat(),
        }
        os.write(descriptor, _json_bytes(lock_payload))
        os.close(descriptor)
        descriptor = -1
        yield
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)


def _backup_database(config: DeadlineRefreshConfig, *, started_at: datetime) -> Path | None:
    if config.backup_directory is None or not config.database_path.is_file():
        return None
    config.backup_directory.mkdir(parents=True, exist_ok=True)
    timestamp = started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = config.backup_directory / (
        f"gw{config.target_gameweek}_{timestamp}_{config.database_path.name}"
    )
    shutil.copy2(config.database_path, destination)
    return destination


def _deliver_alert(
    webhook_url: str | None,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
    post: Callable[..., Any],
) -> dict[str, Any]:
    if webhook_url is None or not webhook_url.strip():
        return {"status": "not_configured"}
    try:
        response = post(webhook_url, json=payload, timeout=timeout_seconds)
        response.raise_for_status()
    except Exception as exc:
        return {
            "status": "failed",
            "error_type": type(exc).__name__,
            "message": "webhook delivery failed",
        }
    return {"status": "delivered", "http_status": int(response.status_code)}


def run_deadline_refresh(
    config: DeadlineRefreshConfig,
    *,
    materialize: Callable[..., ReleaseMaterializationResult] = materialize_inseason_release,
    export: Callable[..., WebReleaseExport] = build_web_release,
    post_alert: Callable[..., Any] = requests.post,
    clock: Callable[[], datetime] | None = None,
) -> DeadlineRefreshResult:
    """Run the full refresh without exposing a partially-built web release.

    Immutable database rows created before a failure remain available for
    diagnosis, but the published compact release changes only after every
    materialization and validation gate has passed.
    """

    now = clock or (lambda: datetime.now(UTC))
    started_at = now()
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    run_id = f"deadline_refresh_{uuid.uuid4().hex[:16]}"
    previous_release_id = _existing_release_id(config.web_release_output)
    stage = "lock"
    stage_started = time.perf_counter()
    report: dict[str, Any]

    with _exclusive_lock(config.lock_file, started_at=started_at):
        backup_path = None
        try:
            stage = "backup"
            backup_path = _backup_database(config, started_at=started_at)
            stage = "materialize"
            materialized = materialize(
                target_gameweek=config.target_gameweek,
                current_season=config.current_season,
                previous_season=config.previous_season,
                team_strength_csv=config.team_strength_csv,
                team_strength_source_label=config.team_strength_source_label,
                vaastav_players_csv=config.vaastav_players_csv,
                vaastav_source_revision=config.vaastav_source_revision,
                calibration_artifact_id=config.calibration_artifact_id,
                uncertainty_artifact_id=config.uncertainty_artifact_id,
                previous_effective_fixtures=config.previous_effective_fixtures,
                allow_analytically_complete=config.allow_analytically_complete,
                database_path=config.database_path,
            )
            _atomic_write(
                config.materialization_report_output,
                _json_bytes(materialized.report),
            )
            if not materialized.passes:
                raise ValueError("materialized release failed validation")

            stage = "export"
            exported = export(
                model_run_ids=materialized.model_run_ids,
                require_production=config.require_production,
                database_path=config.database_path,
            )

            stage = "publish"
            _atomic_write(config.web_release_output, _json_bytes(exported.payload))
            finished_at = now()
            rating_benchmark = exported.payload.get("release", {}).get("rating_benchmark")
            report = {
                "schema_version": "deadline_refresh_status_v1",
                "run_id": run_id,
                "status": "succeeded",
                "stage": "complete",
                "target_gameweek": config.target_gameweek,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "duration_seconds": round(time.perf_counter() - stage_started, 3),
                "previous_release_id": previous_release_id,
                "release_id": exported.release_id,
                "release_health": exported.health,
                "rating_benchmark_id": (
                    None
                    if not isinstance(rating_benchmark, dict)
                    else rating_benchmark.get("artifact_id")
                ),
                "rating_benchmark_status": (
                    None
                    if not isinstance(rating_benchmark, dict)
                    else rating_benchmark.get("status")
                ),
                "model_run_ids": list(materialized.model_run_ids),
                "web_release_output": str(config.web_release_output),
                "materialization_report_output": str(config.materialization_report_output),
                "database_backup": None if backup_path is None else str(backup_path),
            }
        except Exception as exc:
            finished_at = now()
            report = {
                "schema_version": "deadline_refresh_status_v1",
                "run_id": run_id,
                "status": "failed",
                "stage": stage,
                "target_gameweek": config.target_gameweek,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "duration_seconds": round(time.perf_counter() - stage_started, 3),
                "previous_release_id": previous_release_id,
                "release_id": None,
                "web_release_output": str(config.web_release_output),
                "materialization_report_output": str(config.materialization_report_output),
                "database_backup": None if backup_path is None else str(backup_path),
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }

        alert_payload = {
            key: report.get(key)
            for key in (
                "schema_version",
                "run_id",
                "status",
                "stage",
                "target_gameweek",
                "started_at",
                "finished_at",
                "previous_release_id",
                "release_id",
                "release_health",
                "rating_benchmark_id",
                "rating_benchmark_status",
                "error",
            )
            if key in report
        }
        alert_payload["event"] = "fpl_deadline_refresh"
        report["alert"] = _deliver_alert(
            config.alert_webhook_url,
            alert_payload,
            timeout_seconds=config.alert_timeout_seconds,
            post=post_alert,
        )
        _atomic_write(config.status_output, _json_bytes(report))
    return DeadlineRefreshResult(report=report)
