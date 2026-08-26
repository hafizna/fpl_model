"""Surface raw xPts, calibrated xPts, and uncertainty intervals side by side.

Every decision command already reports `expected_points` (== `final_xpts`,
production scoring) and a combined `uncertainty` scalar per player. What is
missing is visibility into the two artifacts sitting alongside production
scoring without ever feeding it: shadow-calibrated xPts
(`player_fixture_shadow_projection`) and the residual-interval lower/upper bound
(`player_fixture_uncertainty`). Both remain measurement-only per
`docs/SPRINT4_UNCERTAINTY_AND_CALIBRATION.md` -- this module reads them for
display, exactly as stored, and changes no ranking, search, or scoring decision
anywhere. It is purely additive: every field it returns is optional metadata a
CLI attaches to its output, never an input to `decision/*` search logic.

Aggregation to one row per `fpl_id` mirrors what `decision/lineup_store.py`
already does for `final_xpts`/`uncertainty` when a player has more than one
fixture in a Gameweek (a double gameweek): xPts-shaped values are summed across
fixtures, and uncertainty/RMSE-shaped values are combined by root-sum-of-squares,
consistent with `lineup_store.load_lineup_inputs`'s own `combined_uncertainty`.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Any

import duckdb


@dataclass(frozen=True, slots=True)
class PlayerTransparency:
    fpl_id: int
    raw_xpts: float
    shadow_calibrated_xpts: float | None
    calibration_artifact_id: str | None
    calibration_status: str | None
    lower_xpts: float | None
    upper_xpts: float | None
    predictive_rmse: float | None
    risk_band: str | None
    uncertainty_artifact_id: str | None
    uncertainty_status: str | None


def load_player_transparency(
    connection: duckdb.DuckDBPyConnection,
    *,
    model_run_id: str,
    fpl_ids: tuple[int, ...],
) -> dict[int, PlayerTransparency]:
    """Load raw/calibrated xPts and uncertainty intervals for the given players.

    ``fpl_ids`` are resolved to ``player_code`` via ``player_snapshot`` against
    the run's own ``source_ingestion_run_id``, the same join every decision
    ``_store.py`` module already uses. A player absent from either shadow table
    (calibration or uncertainty, or both) simply has ``None`` in those fields --
    this is display-only and never raises for missing shadow data, unlike the
    hard coverage gate on production projections.
    """
    if not fpl_ids:
        return {}

    code_rows = connection.execute(
        """
        SELECT ps.fpl_id, ps.player_code
        FROM player_snapshot AS ps
        JOIN model_run AS m ON m.source_ingestion_run_id = ps.ingestion_run_id
        WHERE m.model_run_id = ? AND ps.fpl_id IN ({placeholders})
        """.format(placeholders=",".join("?" * len(fpl_ids))),
        [model_run_id, *fpl_ids],
    ).fetchall()
    code_to_fpl_id = {int(code): int(fpl_id) for fpl_id, code in code_rows}
    if not code_to_fpl_id:
        return {}

    codes = tuple(code_to_fpl_id)
    placeholders = ",".join("?" * len(codes))

    calibration_rows = connection.execute(
        f"""
        SELECT s.player_code, s.raw_xpts, s.shadow_calibrated_xpts,
               s.artifact_id, a.status
        FROM player_fixture_shadow_projection AS s
        JOIN shadow_calibration_artifact AS a USING (artifact_id)
        WHERE s.model_run_id = ? AND s.player_code IN ({placeholders})
        """,
        [model_run_id, *codes],
    ).fetchall()
    calibration_by_code: dict[int, dict[str, Any]] = {}
    for player_code, raw_xpts, calibrated_xpts, artifact_id, status in calibration_rows:
        bucket = calibration_by_code.setdefault(
            int(player_code),
            {"raw_xpts": 0.0, "calibrated_xpts": 0.0, "artifact_id": str(artifact_id), "status": str(status)},
        )
        bucket["raw_xpts"] += float(raw_xpts)
        bucket["calibrated_xpts"] += float(calibrated_xpts)

    uncertainty_rows = connection.execute(
        f"""
        SELECT u.player_code, u.lower_xpts, u.upper_xpts, u.predictive_rmse,
               u.risk_band, u.artifact_id, a.status
        FROM player_fixture_uncertainty AS u
        JOIN uncertainty_artifact AS a USING (artifact_id)
        WHERE u.model_run_id = ? AND u.player_code IN ({placeholders})
        """,
        [model_run_id, *codes],
    ).fetchall()
    risk_rank = {"low": 0, "medium": 1, "high": 2}
    uncertainty_by_code: dict[int, dict[str, Any]] = {}
    for player_code, lower, upper, rmse, risk_band, artifact_id, status in uncertainty_rows:
        bucket = uncertainty_by_code.setdefault(
            int(player_code),
            {
                "lower_xpts": 0.0,
                "upper_xpts": 0.0,
                "rmse_sum_squares": 0.0,
                "risk_band": str(risk_band),
                "artifact_id": str(artifact_id),
                "status": str(status),
            },
        )
        bucket["lower_xpts"] += float(lower)
        bucket["upper_xpts"] += float(upper)
        bucket["rmse_sum_squares"] += float(rmse) ** 2
        if risk_rank.get(str(risk_band), 0) > risk_rank.get(bucket["risk_band"], 0):
            bucket["risk_band"] = str(risk_band)

    result: dict[int, PlayerTransparency] = {}
    for player_code, fpl_id in code_to_fpl_id.items():
        calibration = calibration_by_code.get(player_code)
        uncertainty = uncertainty_by_code.get(player_code)
        result[fpl_id] = PlayerTransparency(
            fpl_id=fpl_id,
            raw_xpts=calibration["raw_xpts"] if calibration else 0.0,
            shadow_calibrated_xpts=calibration["calibrated_xpts"] if calibration else None,
            calibration_artifact_id=calibration["artifact_id"] if calibration else None,
            calibration_status=calibration["status"] if calibration else None,
            lower_xpts=uncertainty["lower_xpts"] if uncertainty else None,
            upper_xpts=uncertainty["upper_xpts"] if uncertainty else None,
            predictive_rmse=sqrt(uncertainty["rmse_sum_squares"]) if uncertainty else None,
            risk_band=uncertainty["risk_band"] if uncertainty else None,
            uncertainty_artifact_id=uncertainty["artifact_id"] if uncertainty else None,
            uncertainty_status=uncertainty["status"] if uncertainty else None,
        )
    return result


def transparency_report(transparency: PlayerTransparency | None) -> dict[str, Any] | None:
    """Render one player's transparency row as a JSON-serialisable dict, or None."""
    if transparency is None:
        return None
    return {
        "raw_xpts": transparency.raw_xpts,
        "shadow_calibrated_xpts": transparency.shadow_calibrated_xpts,
        "calibration_artifact_id": transparency.calibration_artifact_id,
        "calibration_status": transparency.calibration_status,
        "lower_xpts": transparency.lower_xpts,
        "upper_xpts": transparency.upper_xpts,
        "predictive_rmse": transparency.predictive_rmse,
        "risk_band": transparency.risk_band,
        "uncertainty_artifact_id": transparency.uncertainty_artifact_id,
        "uncertainty_status": transparency.uncertainty_status,
    }
