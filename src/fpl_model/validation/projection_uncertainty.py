"""Residual-based predictive intervals for player-fixture xPts.

The artifact is fitted on realised historical residuals. Application never
changes mean xPts; a shadow artifact writes auditable intervals only, while an
explicitly approved artifact may also populate the legacy ``uncertainty`` field.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from itertools import groupby
from math import sqrt
from pathlib import Path
from typing import Literal

import duckdb
import numpy as np

from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database
from fpl_model.validation.backtest import BacktestObservation
from fpl_model.validation.benchwarmers_backtest import ScoredObservationDiagnostics

POLICY_VERSION = "historical_residual_uncertainty_v1"
ALL = "ALL"
RiskBand = Literal["low", "medium", "high"]
CHEAP_ENABLER_MAX_PRICE = {"GK": 4.5, "DEF": 4.5, "MID": 5.5, "FWD": 5.5}
PREMIUM_MIN_PRICE = {"GK": 5.5, "DEF": 6.0, "MID": 10.0, "FWD": 10.0}


@dataclass(frozen=True, slots=True)
class ResidualRow:
    player_code: int
    fixture_id: int
    gameweek: int
    deadline: datetime
    outcome_available_at: datetime
    position: str
    predicted_xpts: float
    start_probability: float
    residual: float


@dataclass(frozen=True, slots=True)
class UncertaintySegment:
    scope: str
    position: str
    xpts_band: str
    start_band: str
    sample_rows: int
    sample_gameweeks: int
    mean_error: float
    predictive_rmse: float
    residual_lower: float
    residual_upper: float


@dataclass(frozen=True, slots=True)
class UncertaintyArtifactResult:
    artifact_id: str
    segment_rows: int
    low_risk_rmse_threshold: float
    high_risk_rmse_threshold: float
    status: str


@dataclass(frozen=True, slots=True)
class AppliedUncertaintyResult:
    model_run_id: str
    artifact_id: str
    player_fixture_rows: int
    application_mode: str


@dataclass(frozen=True, slots=True)
class IntervalEvaluation:
    cohort: str
    observations: int
    empirical_coverage: float
    mean_interval_width: float
    mean_predictive_rmse: float


@dataclass(frozen=True, slots=True)
class WalkForwardInterval:
    player_code: int
    fixture_id: int
    gameweek: int
    actual_points: float
    lower_xpts: float
    upper_xpts: float
    predictive_rmse: float
    segment_scope: str


def xpts_band(value: float) -> str:
    if value < 2.0:
        return "lt2"
    if value < 4.0:
        return "2to4"
    if value < 6.0:
        return "4to6"
    return "gte6"


def start_band(value: float) -> str:
    if value < 0.25:
        return "lt025"
    if value < 0.60:
        return "025to060"
    if value < 0.85:
        return "060to085"
    return "gte085"


def build_residual_rows(
    observations: tuple[BacktestObservation, ...],
    diagnostics: tuple[ScoredObservationDiagnostics, ...],
) -> tuple[ResidualRow, ...]:
    """Join the backtest's predictions and diagnostic features one-to-one."""
    observation_by_key = {
        (int(row.player_id), int(row.fixture_id), row.gameweek): row
        for row in observations
    }
    if len(observation_by_key) != len(observations):
        raise ValueError("backtest observations contain duplicate keys")
    diagnostic_by_key = {
        (row.player_code, row.fixture_id, row.gameweek): row for row in diagnostics
    }
    if len(diagnostic_by_key) != len(diagnostics):
        raise ValueError("backtest diagnostics contain duplicate keys")
    if set(observation_by_key) != set(diagnostic_by_key):
        raise ValueError("backtest observations and diagnostics must have identical keys")
    rows = []
    for key in sorted(observation_by_key, key=lambda item: (item[2], item[1], item[0])):
        observation = observation_by_key[key]
        diagnostic = diagnostic_by_key[key]
        if (
            observation.predicted_xpts != diagnostic.predicted_xpts
            or observation.actual_points != diagnostic.actual_points
        ):
            raise ValueError("observation and diagnostic values disagree")
        rows.append(
            ResidualRow(
                player_code=key[0],
                fixture_id=key[1],
                gameweek=key[2],
                deadline=observation.deadline,
                outcome_available_at=observation.outcome_available_at,
                position=diagnostic.position,
                predicted_xpts=diagnostic.predicted_xpts,
                start_probability=diagnostic.start_probability,
                residual=diagnostic.actual_points - diagnostic.predicted_xpts,
            )
        )
    return tuple(rows)


def _segment(scope: str, key: tuple[str, str, str], rows: list[ResidualRow], interval_mass: float):
    residuals = np.asarray([row.residual for row in rows], dtype=np.float64)
    tail = (1.0 - interval_mass) / 2.0
    lower, upper = np.quantile(residuals, [tail, 1.0 - tail])
    return UncertaintySegment(
        scope=scope,
        position=key[0],
        xpts_band=key[1],
        start_band=key[2],
        sample_rows=len(rows),
        sample_gameweeks=len({row.gameweek for row in rows}),
        mean_error=float(np.mean(residuals)),
        predictive_rmse=float(sqrt(float(np.mean(residuals**2)))),
        residual_lower=float(lower),
        residual_upper=float(upper),
    )


def fit_uncertainty_segments(
    rows: tuple[ResidualRow, ...], *, interval_mass: float = 0.80
) -> tuple[UncertaintySegment, ...]:
    if not rows:
        raise ValueError("at least one residual row is required")
    if not 0.0 < interval_mass < 1.0:
        raise ValueError("interval_mass must be between 0 and 1")
    positions = {"GK", "DEF", "MID", "FWD"}
    if any(row.position not in positions for row in rows):
        raise ValueError("residual positions must use GK/DEF/MID/FWD")
    groups: dict[tuple[str, str, str, str], list[ResidualRow]] = {}
    for row in rows:
        xb = xpts_band(row.predicted_xpts)
        sb = start_band(row.start_probability)
        for scope, key in (
            ("overall", (ALL, ALL, ALL)),
            ("position", (row.position, ALL, ALL)),
            ("position_xpts", (row.position, xb, ALL)),
            ("position_xpts_start", (row.position, xb, sb)),
        ):
            groups.setdefault((scope, *key), []).append(row)
    return tuple(
        _segment(scope, (position, xb, sb), grouped, interval_mass)
        for (scope, position, xb, sb), grouped in sorted(groups.items())
    )


def _choose_segment(
    segments: tuple[UncertaintySegment, ...],
    *,
    position: str,
    predicted_xpts: float,
    start_probability: float,
    minimum_segment_rows: int,
    minimum_segment_gameweeks: int,
) -> UncertaintySegment | None:
    index = {
        (row.scope, row.position, row.xpts_band, row.start_band): row for row in segments
    }
    xb = xpts_band(predicted_xpts)
    sb = start_band(start_probability)
    candidates = (
        ("position_xpts_start", position, xb, sb),
        ("position_xpts", position, xb, ALL),
        ("position", position, ALL, ALL),
        ("overall", ALL, ALL, ALL),
    )
    return next(
        (
            index[key]
            for key in candidates
            if key in index
            and index[key].sample_rows >= minimum_segment_rows
            and index[key].sample_gameweeks >= minimum_segment_gameweeks
        ),
        None,
    )


def walk_forward_intervals(
    rows: tuple[ResidualRow, ...],
    *,
    interval_mass: float = 0.80,
    minimum_segment_rows: int = 100,
    minimum_segment_gameweeks: int = 5,
) -> tuple[WalkForwardInterval, ...]:
    """Evaluate intervals using only outcomes available before each deadline."""
    output = []
    ordered = sorted(rows, key=lambda row: (row.deadline, row.gameweek, row.fixture_id, row.player_code))
    for (_, target_gameweek), target_group in groupby(
        ordered, key=lambda row: (row.deadline, row.gameweek)
    ):
        targets = tuple(target_group)
        target_deadline = targets[0].deadline
        history = tuple(
            row
            for row in rows
            if row.gameweek < target_gameweek
            and row.outcome_available_at <= target_deadline
        )
        if not history:
            continue
        segments = fit_uncertainty_segments(history, interval_mass=interval_mass)
        for target in targets:
            chosen = _choose_segment(
                segments,
                position=target.position,
                predicted_xpts=target.predicted_xpts,
                start_probability=target.start_probability,
                minimum_segment_rows=minimum_segment_rows,
                minimum_segment_gameweeks=minimum_segment_gameweeks,
            )
            if chosen is None:
                continue
            lower = max(0.0, target.predicted_xpts + chosen.residual_lower)
            output.append(
                WalkForwardInterval(
                    player_code=target.player_code,
                    fixture_id=target.fixture_id,
                    gameweek=target.gameweek,
                    actual_points=target.predicted_xpts + target.residual,
                    lower_xpts=lower,
                    upper_xpts=max(
                        lower,
                        target.predicted_xpts + chosen.residual_upper,
                    ),
                    predictive_rmse=chosen.predictive_rmse,
                    segment_scope=chosen.scope,
                )
            )
    return tuple(output)


def store_uncertainty_artifact(
    rows: tuple[ResidualRow, ...],
    *,
    source_season: str,
    source_model_version: str,
    source_reference: str,
    interval_mass: float = 0.80,
    minimum_segment_rows: int = 100,
    minimum_segment_gameweeks: int = 5,
    status: str = "shadow",
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> UncertaintyArtifactResult:
    """Fit and store one immutable historical residual artifact."""
    if status not in {"shadow", "approved", "rejected"}:
        raise ValueError("unsupported uncertainty artifact status")
    if minimum_segment_rows < 1 or minimum_segment_gameweeks < 1:
        raise ValueError("minimum segment support must be positive")
    if not source_season.strip() or not source_model_version.strip():
        raise ValueError("source season and model version must not be blank")
    if not source_reference.strip():
        raise ValueError("source_reference must not be blank")
    segments = fit_uncertainty_segments(rows, interval_mass=interval_mass)
    overall = next(segment for segment in segments if segment.scope == "overall")
    if (
        overall.sample_rows < minimum_segment_rows
        or overall.sample_gameweeks < minimum_segment_gameweeks
    ):
        raise ValueError("overall residual history does not meet the minimum support gate")
    eligible_specific = [
        segment.predictive_rmse
        for segment in segments
        if segment.scope == "position_xpts_start"
        and segment.sample_rows >= minimum_segment_rows
        and segment.sample_gameweeks >= minimum_segment_gameweeks
    ]
    threshold_values = eligible_specific or [overall.predictive_rmse]
    low_threshold, high_threshold = np.quantile(threshold_values, [1 / 3, 2 / 3])
    segment_payload = [
        {
            "scope": row.scope,
            "position": row.position,
            "xpts_band": row.xpts_band,
            "start_band": row.start_band,
            "sample_rows": row.sample_rows,
            "sample_gameweeks": row.sample_gameweeks,
            "mean_error": row.mean_error,
            "predictive_rmse": row.predictive_rmse,
            "residual_lower": row.residual_lower,
            "residual_upper": row.residual_upper,
        }
        for row in segments
    ]
    identity = json.dumps(
        {
            "source_season": source_season,
            "source_model_version": source_model_version,
            "source_reference": source_reference,
            "policy_version": POLICY_VERSION,
            "interval_mass": interval_mass,
            "minimum_segment_rows": minimum_segment_rows,
            "minimum_segment_gameweeks": minimum_segment_gameweeks,
            "status": status,
            "segments": segment_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    artifact_id = f"uncertainty_{hashlib.sha256(identity).hexdigest()[:16]}"
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        existing = connection.execute(
            "SELECT segment_rows, low_risk_rmse_threshold, high_risk_rmse_threshold, status "
            "FROM uncertainty_artifact WHERE artifact_id = ?",
            [artifact_id],
        ).fetchone()
        if existing is not None:
            if str(existing[3]) != status:
                raise ValueError("artifact identity already exists with a different status")
            return UncertaintyArtifactResult(
                artifact_id, int(existing[0]), float(existing[1]), float(existing[2]), str(existing[3])
            )
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT INTO uncertainty_artifact VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp
                )
                """,
                [
                    artifact_id,
                    source_season,
                    source_model_version,
                    source_reference,
                    POLICY_VERSION,
                    interval_mass,
                    minimum_segment_rows,
                    minimum_segment_gameweeks,
                    float(low_threshold),
                    float(high_threshold),
                    len(segments),
                    status,
                ],
            )
            connection.executemany(
                "INSERT INTO uncertainty_segment VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        artifact_id,
                        row.scope,
                        row.position,
                        row.xpts_band,
                        row.start_band,
                        row.sample_rows,
                        row.sample_gameweeks,
                        row.mean_error,
                        row.predictive_rmse,
                        row.residual_lower,
                        row.residual_upper,
                    )
                    for row in segments
                ],
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")
    return UncertaintyArtifactResult(
        artifact_id, len(segments), float(low_threshold), float(high_threshold), status
    )


def _risk_band(rmse: float, low: float, high: float) -> RiskBand:
    if rmse <= low:
        return "low"
    if rmse <= high:
        return "medium"
    return "high"


def _escalate_risk(band: RiskBand) -> RiskBand:
    return {"low": "medium", "medium": "high", "high": "high"}[band]  # type: ignore[return-value]


def _requires_risk_escalation(flags: set[str]) -> bool:
    markers = (
        "PROMOTED_PRIOR",
        "CURRENT_SEASON_APPEARANCE_ONLY",
        "EMPIRICAL_",
        "POSITION_CHANGED_",
        "MISSING_",
        "NO_PRIOR_TACTICAL_ROLE_CONTEXT",
    )
    return any(any(marker in flag for marker in markers) for flag in flags)


def apply_uncertainty_artifact(
    *,
    model_run_id: str,
    artifact_id: str,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> AppliedUncertaintyResult:
    """Attach intervals to a run; only approved artifacts activate the scalar field."""
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        artifact = connection.execute(
            """
            SELECT minimum_segment_rows, minimum_segment_gameweeks,
                   low_risk_rmse_threshold, high_risk_rmse_threshold, status
            FROM uncertainty_artifact WHERE artifact_id = ?
            """,
            [artifact_id],
        ).fetchone()
        if artifact is None:
            raise ValueError(f"unknown uncertainty artifact: {artifact_id}")
        min_rows, min_gameweeks, low_threshold, high_threshold, status = artifact
        if status == "rejected":
            raise ValueError("rejected uncertainty artifacts cannot be applied")
        model = connection.execute(
            "SELECT source_ingestion_run_id FROM model_run WHERE model_run_id = ? AND status = 'completed'",
            [model_run_id],
        ).fetchone()
        if model is None:
            raise ValueError("model run must exist and be completed")
        segments = {
            (row[0], row[1], row[2], row[3]): row[4:]
            for row in connection.execute(
                """
                SELECT scope, position, xpts_band, start_band, sample_rows,
                       sample_gameweeks, predictive_rmse, residual_lower, residual_upper
                FROM uncertainty_segment WHERE artifact_id = ?
                """,
                [artifact_id],
            ).fetchall()
        }
        projections = connection.execute(
            """
            SELECT p.player_code, p.fixture_id, ps.fpl_position, p.start_probability,
                   p.final_xpts, p.data_quality_flags
            FROM player_fixture_projection AS p
            JOIN model_run AS m USING (model_run_id)
            JOIN player_snapshot AS ps
              ON ps.ingestion_run_id = m.source_ingestion_run_id
             AND ps.player_code = p.player_code
            WHERE p.model_run_id = ?
            ORDER BY p.player_code, p.fixture_id
            """,
            [model_run_id],
        ).fetchall()
        if not projections:
            raise ValueError("model run has no player-fixture projections")
        output = []
        active_updates = []
        for player_code, fixture_id, position, start, xpts, raw_flags in projections:
            xb = xpts_band(float(xpts))
            sb = start_band(float(start))
            candidates = (
                ("position_xpts_start", position, xb, sb),
                ("position_xpts", position, xb, ALL),
                ("position", position, ALL, ALL),
                ("overall", ALL, ALL, ALL),
            )
            chosen_key = next(
                (
                    key
                    for key in candidates
                    if key in segments
                    and int(segments[key][0]) >= int(min_rows)
                    and int(segments[key][1]) >= int(min_gameweeks)
                ),
                None,
            )
            if chosen_key is None:
                raise ValueError("no uncertainty segment passes the artifact support gate")
            sample_rows, _, rmse, residual_lower, residual_upper = segments[chosen_key]
            flags = set(json.loads(raw_flags or "[]"))
            auxiliary_flags: set[str] = set()
            risk = _risk_band(float(rmse), float(low_threshold), float(high_threshold))
            if _requires_risk_escalation(flags):
                risk = _escalate_risk(risk)
                auxiliary_flags.add("RISK_ESCALATED_FOR_CONTEXT_OR_PRIOR_GAP")
            lower = max(0.0, float(xpts) + float(residual_lower))
            upper = max(lower, float(xpts) + float(residual_upper))
            relative = float(rmse) / max(float(xpts), 1.0)
            output.append(
                (
                    model_run_id,
                    player_code,
                    fixture_id,
                    artifact_id,
                    lower,
                    upper,
                    float(rmse),
                    relative,
                    risk,
                    chosen_key[0],
                    int(sample_rows),
                    json.dumps(sorted(auxiliary_flags)),
                )
            )
            active_updates.append((float(rmse), model_run_id, player_code, fixture_id))
        mode = "active" if status == "approved" else "shadow"
        existing = connection.execute(
            "SELECT artifact_id, application_mode FROM model_uncertainty_lineage WHERE model_run_id = ?",
            [model_run_id],
        ).fetchone()
        if existing is not None:
            if existing != (artifact_id, mode):
                raise ValueError("model run already has different uncertainty lineage")
            return AppliedUncertaintyResult(model_run_id, artifact_id, len(output), mode)
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                "INSERT INTO model_uncertainty_lineage VALUES (?, ?, ?)",
                [model_run_id, artifact_id, mode],
            )
            connection.executemany(
                "INSERT INTO player_fixture_uncertainty VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                output,
            )
            if mode == "active":
                connection.executemany(
                    """
                    UPDATE player_fixture_projection SET uncertainty = ?
                    WHERE model_run_id = ? AND player_code = ? AND fixture_id = ?
                    """,
                    active_updates,
                )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")
    return AppliedUncertaintyResult(model_run_id, artifact_id, len(output), mode)


def evaluate_intervals(
    rows: tuple[tuple[str, float, float, float, float], ...]
) -> tuple[IntervalEvaluation, ...]:
    """Evaluate coverage by supplied cohort label.

    Each row is ``(cohort, actual_points, lower_xpts, upper_xpts, rmse)``.
    Callers may label premiums, enablers, promoted players, new signings, or
    role changes without coupling this measurement utility to one data source.
    """
    if not rows:
        raise ValueError("at least one interval row is required")
    grouped: dict[str, list[tuple[float, float, float, float]]] = {}
    for cohort, actual, lower, upper, rmse in rows:
        if not cohort.strip() or lower < 0.0 or upper < lower or rmse < 0.0:
            raise ValueError("invalid interval evaluation row")
        grouped.setdefault(cohort, []).append((actual, lower, upper, rmse))
    return tuple(
        IntervalEvaluation(
            cohort=cohort,
            observations=len(values),
            empirical_coverage=sum(lower <= actual <= upper for actual, lower, upper, _ in values)
            / len(values),
            mean_interval_width=sum(upper - lower for _, lower, upper, _ in values) / len(values),
            mean_predictive_rmse=sum(rmse for *_, rmse in values) / len(values),
        )
        for cohort, values in sorted(grouped.items())
    )


def projection_cohorts(
    *, position: str, price: float, data_quality_flags: tuple[str, ...]
) -> tuple[str, ...]:
    """Assign stable production cohorts for later prospective validation."""
    if position not in CHEAP_ENABLER_MAX_PRICE or price <= 0.0:
        raise ValueError("invalid projection position or price")
    cohorts = {"all", f"position_{position}"}
    if price <= CHEAP_ENABLER_MAX_PRICE[position]:
        cohorts.add("cheap_enabler")
    if price >= PREMIUM_MIN_PRICE[position]:
        cohorts.add("premium")
    flags = set(data_quality_flags)
    if any("PROMOTED_PRIOR" in flag for flag in flags):
        cohorts.add("promoted_team")
    if any(
        marker in flag
        for flag in flags
        for marker in (
            "CURRENT_SEASON_APPEARANCE_ONLY",
            "NO_PREVIOUS_PL_PLAYER_RATE_HISTORY",
        )
    ):
        cohorts.add("new_signing_or_current_only")
    if any("POSITION_CHANGED_" in flag for flag in flags):
        cohorts.add("position_change")
    return tuple(sorted(cohorts))
