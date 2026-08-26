"""Fail closed unless every released projection has APPROVED calibration/uncertainty.

Two artifact kinds sit above the raw component xPts: a calibration slope/intercept
(`shadow_calibration_artifact`) and residual-interval uncertainty
(`uncertainty_artifact`). Both are currently stuck at `status='shadow'` for every
artifact in this database -- see `docs/SPRINT4_UNCERTAINTY_AND_CALIBRATION.md`.
They were fit and measured on the same 2025/26 season they would be applied to,
which is exactly the leakage risk `status` exists to guard against: promotion to
`approved` requires an independent-season or prospectively frozen 2026/27
confirmatory evaluation (Sprint 4's own unchecked item), not yet performed.

This module checks the state as it actually is, not as Sprint 5 will eventually
want it to be. Today, for every real release, it is EXPECTED to fail closed --
that is the correct, honest answer, not a bug. It exists so that the day an
artifact is legitimately promoted to `approved` (a decision this module does not
make and has no opinion on how to reach), a release automatically starts passing
without any change to this file.

`apply_uncertainty_artifact` already only activates the row-level
`player_fixture_projection.uncertainty` scalar when the artifact's status was
`approved` at application time (recorded as `model_uncertainty_lineage.
application_mode = 'active'`); this module checks that same condition rather than
re-deriving it, plus the calibration artifact's own `status` directly, since
`model_shadow_calibration_lineage` carries no equivalent mode column.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from fpl_model.storage import DEFAULT_DATABASE_PATH


@dataclass(frozen=True, slots=True)
class ReleaseApprovalReport:
    report: dict[str, Any]

    @property
    def passes(self) -> bool:
        return bool(self.report["passes"])


def _row(connection: duckdb.DuckDBPyConnection, sql: str, params: list[Any]) -> tuple | None:
    return connection.execute(sql, params).fetchone()


def _gameweek_check(connection: duckdb.DuckDBPyConnection, model_run_id: str) -> dict[str, Any]:
    model_run = _row(
        connection,
        "SELECT target_gameweek FROM model_run WHERE model_run_id = ?",
        [model_run_id],
    )
    if model_run is None:
        raise ValueError(f"unknown model_run_id: {model_run_id}")
    target_gameweek = int(model_run[0])

    projection_row_count = _row(
        connection,
        "SELECT count(*) FROM player_fixture_projection WHERE model_run_id = ?",
        [model_run_id],
    )
    total_rows = int(projection_row_count[0]) if projection_row_count else 0

    uncertainty = _row(
        connection,
        """
        SELECT a.artifact_id, a.status, l.application_mode,
               (SELECT count(*) FROM player_fixture_projection
                WHERE model_run_id = ? AND uncertainty IS NOT NULL) AS populated_rows
        FROM model_uncertainty_lineage AS l
        JOIN uncertainty_artifact AS a USING (artifact_id)
        WHERE l.model_run_id = ?
        """,
        [model_run_id, model_run_id],
    )
    calibration = _row(
        connection,
        """
        SELECT a.artifact_id, a.status
        FROM model_shadow_calibration_lineage AS l
        JOIN shadow_calibration_artifact AS a USING (artifact_id)
        WHERE l.model_run_id = ?
        """,
        [model_run_id],
    )

    problems: list[str] = []

    if uncertainty is None:
        problems.append(f"GW{target_gameweek}: no uncertainty artifact is linked to this run")
        uncertainty_report = None
    else:
        artifact_id, status, application_mode, populated_rows = uncertainty
        populated_rows = int(populated_rows)
        is_approved_and_active = status == "approved" and application_mode == "active"
        if not is_approved_and_active:
            problems.append(
                f"GW{target_gameweek}: uncertainty artifact {artifact_id} is "
                f"status={status!r} application_mode={application_mode!r}, not an "
                "approved+active artifact"
            )
        elif populated_rows != total_rows:
            problems.append(
                f"GW{target_gameweek}: uncertainty artifact {artifact_id} is approved+active "
                f"but only {populated_rows}/{total_rows} projection rows have a populated "
                "uncertainty scalar"
            )
        uncertainty_report = {
            "artifact_id": str(artifact_id),
            "status": str(status),
            "application_mode": str(application_mode),
            "populated_rows": populated_rows,
            "total_rows": total_rows,
        }

    if calibration is None:
        problems.append(f"GW{target_gameweek}: no shadow-calibration artifact is linked to this run")
        calibration_report = None
    else:
        artifact_id, status = calibration
        if status != "approved":
            problems.append(
                f"GW{target_gameweek}: calibration artifact {artifact_id} is "
                f"status={status!r}, not approved"
            )
        calibration_report = {"artifact_id": str(artifact_id), "status": str(status)}

    return {
        "target_gameweek": target_gameweek,
        "model_run_id": model_run_id,
        "total_projection_rows": total_rows,
        "uncertainty": uncertainty_report,
        "calibration": calibration_report,
        "problems": problems,
    }


def check_release_approval(
    *,
    model_run_ids: tuple[str, ...],
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> ReleaseApprovalReport:
    """Check that every released projection row has APPROVED calibration/uncertainty.

    Fails closed (``passes=False``) unless, for every ``model_run_id`` given, both
    an approved+active uncertainty artifact populates every projection row's
    ``uncertainty`` scalar AND an approved calibration artifact is linked. Given
    the current state of every artifact in this database (``status='shadow'``
    only), this is expected to fail today -- see this module's docstring.
    """
    if not model_run_ids:
        raise ValueError("check_release_approval requires at least one model_run_id")
    if len(set(model_run_ids)) != len(model_run_ids):
        raise ValueError("model_run_ids contains duplicates")

    with duckdb.connect(str(database_path), read_only=True) as connection:
        gameweeks = [_gameweek_check(connection, run_id) for run_id in model_run_ids]

    all_problems = [problem for gw in gameweeks for problem in gw["problems"]]
    passes = not all_problems

    payload: dict[str, Any] = {
        "label": "release_approval_gate_v1",
        "model_run_ids": list(model_run_ids),
        "passes": passes,
        "problems": all_problems,
        "gameweeks": gameweeks,
        "note": (
            "Every artifact in this database currently has status='shadow'. This gate is "
            "expected to fail until Sprint 4's independent-season or prospectively frozen "
            "2026/27 confirmatory evaluation promotes a calibration/uncertainty artifact to "
            "status='approved' -- see docs/SPRINT4_UNCERTAINTY_AND_CALIBRATION.md. That is "
            "the correct current answer, not a defect in this check."
        ),
        "limitations": [
            "This gate checks artifact approval status and row-level population only. It does "
            "not itself evaluate whether an artifact SHOULD be approved -- that is Sprint 4's "
            "confirmatory-evaluation work, not this module's job.",
            "A calibration artifact currently has no row-level 'applied' scalar to check "
            "(shadow calibration only ever populates player_fixture_shadow_projection, never "
            "final_xpts); this gate checks its lineage/status only.",
        ],
    }
    return ReleaseApprovalReport(report=payload)
