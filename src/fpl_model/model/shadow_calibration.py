"""Immutable xPts calibration artifacts applied in non-scoring shadow mode."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import sqrt
from pathlib import Path

import duckdb

from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database

POLICY_VERSION = "xpts_shadow_calibration_v1"


@dataclass(frozen=True, slots=True)
class ShadowCalibrationArtifactResult:
    artifact_id: str
    status: str


@dataclass(frozen=True, slots=True)
class ShadowCalibrationRunResult:
    model_run_id: str
    artifact_id: str
    player_fixture_rows: int


@dataclass(frozen=True, slots=True)
class ShadowCalibrationEvaluation:
    cohort: str
    observations: int
    raw_mae: float
    shadow_mae: float
    mae_improvement: float
    raw_rmse: float
    shadow_rmse: float
    rmse_improvement: float


def store_shadow_calibration_artifact(
    *,
    source_season: str,
    source_model_version: str,
    source_reference: str,
    training_rows: int,
    training_gameweeks: int,
    slope: float,
    intercept: float,
    status: str = "shadow",
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> ShadowCalibrationArtifactResult:
    """Store a content-addressed fit; creation never activates it."""
    if status not in {"shadow", "approved", "rejected"}:
        raise ValueError("unsupported calibration artifact status")
    if not source_season.strip() or not source_model_version.strip():
        raise ValueError("source season and model version must not be blank")
    if not source_reference.strip():
        raise ValueError("source_reference must not be blank")
    if training_rows < 1 or training_gameweeks < 1:
        raise ValueError("training support must be positive")
    if slope < 0.0:
        raise ValueError("calibration slope must be non-negative")
    identity = json.dumps(
        {
            "calibration_type": "xpts",
            "source_season": source_season,
            "source_model_version": source_model_version,
            "source_reference": source_reference,
            "training_rows": training_rows,
            "training_gameweeks": training_gameweeks,
            "slope": slope,
            "intercept": intercept,
            "policy_version": POLICY_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    artifact_id = f"calibration_{hashlib.sha256(identity).hexdigest()[:16]}"
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        existing = connection.execute(
            "SELECT status FROM shadow_calibration_artifact WHERE artifact_id = ?",
            [artifact_id],
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO shadow_calibration_artifact VALUES (
                    ?, 'xpts', ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp
                )
                """,
                [
                    artifact_id,
                    source_season,
                    source_model_version,
                    source_reference,
                    training_rows,
                    training_gameweeks,
                    slope,
                    intercept,
                    POLICY_VERSION,
                    status,
                ],
            )
        elif str(existing[0]) != status:
            raise ValueError("artifact identity already exists with a different status")
    return ShadowCalibrationArtifactResult(artifact_id, status)


def materialize_shadow_calibration(
    *,
    model_run_id: str,
    artifact_id: str,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> ShadowCalibrationRunResult:
    """Write counterfactual calibrated xPts while leaving production xPts unchanged."""
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        artifact = connection.execute(
            """
            SELECT slope, intercept, status FROM shadow_calibration_artifact
            WHERE artifact_id = ?
            """,
            [artifact_id],
        ).fetchone()
        if artifact is None:
            raise ValueError(f"unknown calibration artifact: {artifact_id}")
        slope, intercept, status = artifact
        if status == "rejected":
            raise ValueError("rejected calibration artifacts cannot run in shadow mode")
        projections = connection.execute(
            """
            SELECT player_code, fixture_id, final_xpts
            FROM player_fixture_projection WHERE model_run_id = ?
            ORDER BY player_code, fixture_id
            """,
            [model_run_id],
        ).fetchall()
        if not projections:
            raise ValueError("model run has no player-fixture projections")
        output = []
        for player_code, fixture_id, raw_xpts in projections:
            unbounded = float(intercept) + float(slope) * float(raw_xpts)
            output.append(
                (
                    model_run_id,
                    player_code,
                    fixture_id,
                    artifact_id,
                    float(raw_xpts),
                    max(0.0, unbounded),
                    unbounded < 0.0,
                )
            )
        existing = connection.execute(
            "SELECT artifact_id FROM model_shadow_calibration_lineage WHERE model_run_id = ?",
            [model_run_id],
        ).fetchone()
        if existing is not None:
            if existing[0] != artifact_id:
                raise ValueError("model run already has different shadow calibration lineage")
            return ShadowCalibrationRunResult(model_run_id, artifact_id, len(output))
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                "INSERT INTO model_shadow_calibration_lineage VALUES (?, ?)",
                [model_run_id, artifact_id],
            )
            connection.executemany(
                "INSERT INTO player_fixture_shadow_projection VALUES (?, ?, ?, ?, ?, ?, ?)",
                output,
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")
    return ShadowCalibrationRunResult(model_run_id, artifact_id, len(output))


def evaluate_shadow_calibration(
    rows: tuple[tuple[str, float, float, float], ...]
) -> tuple[ShadowCalibrationEvaluation, ...]:
    """Compare raw and shadow xPts by caller-supplied prospective cohort."""
    if not rows:
        raise ValueError("at least one shadow calibration row is required")
    grouped: dict[str, list[tuple[float, float, float]]] = {}
    for cohort, actual, raw, shadow in rows:
        if not cohort.strip() or raw < 0.0 or shadow < 0.0:
            raise ValueError("invalid shadow calibration evaluation row")
        grouped.setdefault(cohort, []).append((actual, raw, shadow))
    output = []
    for cohort, values in sorted(grouped.items()):
        raw_errors = [raw - actual for actual, raw, _ in values]
        shadow_errors = [shadow - actual for actual, _, shadow in values]
        count = len(values)
        raw_mae = sum(abs(value) for value in raw_errors) / count
        shadow_mae = sum(abs(value) for value in shadow_errors) / count
        raw_rmse = sqrt(sum(value**2 for value in raw_errors) / count)
        shadow_rmse = sqrt(sum(value**2 for value in shadow_errors) / count)
        output.append(
            ShadowCalibrationEvaluation(
                cohort,
                count,
                raw_mae,
                shadow_mae,
                raw_mae - shadow_mae,
                raw_rmse,
                shadow_rmse,
                raw_rmse - shadow_rmse,
            )
        )
    return tuple(output)
