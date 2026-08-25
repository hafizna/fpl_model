from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from fpl_model.ingest.penalty_review import (
    PlayerPenaltyEvent,
    store_event_penalty_review,
)
from fpl_model.model.shadow_calibration import (
    evaluate_shadow_calibration,
    materialize_shadow_calibration,
    store_shadow_calibration_artifact,
)
from fpl_model.storage import initialize_database
from fpl_model.validation.backtest import BacktestObservation
from fpl_model.validation.benchwarmers_backtest import ScoredObservationDiagnostics
from fpl_model.validation.projection_uncertainty import (
    apply_uncertainty_artifact,
    build_residual_rows,
    evaluate_intervals,
    fit_uncertainty_segments,
    projection_cohorts,
    store_uncertainty_artifact,
    walk_forward_intervals,
)


def _history(*, gameweeks: int = 8, rows_per_gameweek: int = 12):
    observations = []
    diagnostics = []
    base = datetime(2025, 8, 1, 12, tzinfo=UTC)
    positions = ("GK", "DEF", "MID", "FWD")
    for gameweek in range(1, gameweeks + 1):
        deadline = base + timedelta(days=7 * gameweek)
        kickoff = deadline + timedelta(hours=2)
        for index in range(rows_per_gameweek):
            player_code = 1000 + index
            fixture_id = gameweek * 100 + index
            position = positions[index % len(positions)]
            predicted = 1.0 + index % 7
            actual = max(0.0, predicted + ((index + gameweek) % 5 - 2))
            start = (index % 5) / 4
            observation = BacktestObservation(
                season="2025-26",
                gameweek=gameweek,
                deadline=deadline,
                fixture_kickoff=kickoff,
                feature_cutoff=deadline,
                outcome_available_at=kickoff + timedelta(hours=3),
                player_id=player_code,
                fixture_id=fixture_id,
                predicted_xpts=predicted,
                actual_points=actual,
            )
            observations.append(observation)
            diagnostics.append(
                ScoredObservationDiagnostics(
                    player_code=player_code,
                    fixture_id=fixture_id,
                    gameweek=gameweek,
                    position=position,
                    start_probability=start,
                    expected_minutes=start * 80,
                    predicted_xpts=predicted,
                    actual_points=actual,
                    component_appearance=0.0,
                    component_sixty_minutes=0.0,
                    component_saves=0.0,
                    component_yellow_cards=0.0,
                    component_red_cards=0.0,
                    component_bonus=0.0,
                    component_assists=0.0,
                    component_goals=0.0,
                    component_clean_sheet=0.0,
                    component_goals_conceded=0.0,
                    component_defcon=0.0,
                )
            )
    return tuple(observations), tuple(diagnostics)


def _seed_projection(database_path) -> None:
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('snapshot', 'official_fpl_api',
                      '2026-08-20T00:00:00+00:00', 'completed');
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES (
                'snapshot', '2026-27', 1, 1001, 'A', 'Player', 'Player',
                1, 'MID', 8.0, 'a'
            );
            INSERT INTO model_run (
                model_run_id, target_gameweek, as_of, deadline, model_version,
                source_ingestion_run_id, status, completed_at
            ) VALUES (
                'model', 2, '2026-08-20T00:00:00+00:00',
                '2026-08-29T00:00:00+00:00', 'test', 'snapshot',
                'completed', current_timestamp
            );
            INSERT INTO player_fixture_projection VALUES (
                'model', 1001, 101, 1, 2, TRUE, 0.9, 0.05, 82.0,
                5.0, 0.0, 5.0, NULL, '["CURRENT_SEASON_APPEARANCE_ONLY"]'
            );
            """
        )


def test_walk_forward_uncertainty_never_uses_current_or_future_outcomes():
    observations, diagnostics = _history()
    rows = build_residual_rows(observations, diagnostics)
    baseline = walk_forward_intervals(
        rows, minimum_segment_rows=12, minimum_segment_gameweeks=3
    )
    mutated_observations = tuple(
        BacktestObservation(
            season=row.season,
            gameweek=row.gameweek,
            deadline=row.deadline,
            fixture_kickoff=row.fixture_kickoff,
            feature_cutoff=row.feature_cutoff,
            outcome_available_at=row.outcome_available_at,
            player_id=row.player_id,
            fixture_id=row.fixture_id,
            predicted_xpts=row.predicted_xpts,
            actual_points=row.actual_points + 100 if row.gameweek == 8 else row.actual_points,
        )
        for row in observations
    )
    mutated_diagnostics = tuple(
        ScoredObservationDiagnostics(
            **{
                field: (
                    getattr(row, field) + 100
                    if field == "actual_points" and row.gameweek == 8
                    else getattr(row, field)
                )
                for field in row.__dataclass_fields__
            }
        )
        for row in diagnostics
    )
    mutated = walk_forward_intervals(
        build_residual_rows(mutated_observations, mutated_diagnostics),
        minimum_segment_rows=12,
        minimum_segment_gameweeks=3,
    )

    assert baseline
    assert [row for row in baseline if row.gameweek < 8] == [
        row for row in mutated if row.gameweek < 8
    ]


def test_uncertainty_artifact_is_shadow_by_default_and_escalates_context_risk(tmp_path):
    observations, diagnostics = _history()
    residuals = build_residual_rows(observations, diagnostics)
    assert any(row.scope == "position_xpts_start" for row in fit_uncertainty_segments(residuals))
    database_path = tmp_path / "model.duckdb"
    _seed_projection(database_path)
    artifact = store_uncertainty_artifact(
        residuals,
        source_season="2025-26",
        source_model_version="test-backtest",
        source_reference="committed-test",
        minimum_segment_rows=12,
        minimum_segment_gameweeks=3,
        database_path=database_path,
    )
    applied = apply_uncertainty_artifact(
        model_run_id="model",
        artifact_id=artifact.artifact_id,
        database_path=database_path,
    )
    repeated = apply_uncertainty_artifact(
        model_run_id="model",
        artifact_id=artifact.artifact_id,
        database_path=database_path,
    )

    assert repeated == applied
    assert applied.application_mode == "shadow"
    with duckdb.connect(str(database_path), read_only=True) as connection:
        scalar = connection.execute(
            "SELECT uncertainty FROM player_fixture_projection"
        ).fetchone()[0]
        interval = connection.execute(
            """
            SELECT lower_xpts, upper_xpts, risk_band, data_quality_flags
            FROM player_fixture_uncertainty
            """
        ).fetchone()
    assert scalar is None
    assert 0.0 <= interval[0] <= interval[1]
    assert interval[2] in {"medium", "high"}
    assert "RISK_ESCALATED_FOR_CONTEXT_OR_PRIOR_GAP" in json.loads(interval[3])


def test_shadow_calibration_never_changes_production_xpts(tmp_path):
    database_path = tmp_path / "model.duckdb"
    _seed_projection(database_path)
    artifact = store_shadow_calibration_artifact(
        source_season="2025-26",
        source_model_version="test",
        source_reference="test-result.json",
        training_rows=100,
        training_gameweeks=10,
        slope=0.8,
        intercept=0.2,
        database_path=database_path,
    )
    result = materialize_shadow_calibration(
        model_run_id="model",
        artifact_id=artifact.artifact_id,
        database_path=database_path,
    )
    repeated = materialize_shadow_calibration(
        model_run_id="model",
        artifact_id=artifact.artifact_id,
        database_path=database_path,
    )

    assert repeated == result
    with duckdb.connect(str(database_path), read_only=True) as connection:
        values = connection.execute(
            """
            SELECT p.final_xpts, s.raw_xpts, s.shadow_calibrated_xpts
            FROM player_fixture_projection AS p
            JOIN player_fixture_shadow_projection AS s
              USING (model_run_id, player_code, fixture_id)
            """
        ).fetchone()
    assert values == pytest.approx((5.0, 5.0, 4.2))


def test_complete_penalty_review_splits_xg_without_guessing(tmp_path):
    database_path = tmp_path / "model.duckdb"
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('snapshot', 'official_fpl_api',
                      '2026-08-24T00:00:00+00:00', 'completed');
            INSERT INTO fpl_event_live_run VALUES (
                'live', 'snapshot', '2026-27', 1,
                '2026-08-24T01:00:00+00:00', 'event.json', 'sha',
                TRUE, TRUE, 2, 'completed', current_timestamp
            );
            INSERT INTO player_gameweek_stat VALUES
                ('live', 1, 1001, TRUE, 90, 1, 1, 0, 0, 0, 0,
                 0, 20, 2, 0.85, 0.1, 0.5, 8, FALSE, '[]'),
                ('live', 2, 1002, TRUE, 90, 1, 1, 0, 0, 0, 0,
                 0, 20, 2, 0.30, 0.1, 0.5, 7, FALSE, '[]');
            """
        )
    kwargs = {
        "live_run_id": "live",
        "observed_at": datetime(2026, 8, 24, 2, tzinfo=UTC),
        "source_reference": "reviewed match report",
        "rationale": "complete event penalty ledger",
        "penalty_events": (PlayerPenaltyEvent(1, 1, 1, 0.79),),
        "database_path": database_path,
    }
    result = store_event_penalty_review(**kwargs)
    repeated = store_event_penalty_review(**kwargs)

    assert repeated == result
    assert result.penalty_takers == 1
    with duckdb.connect(str(database_path), read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT fpl_id, total_expected_goals, penalty_expected_goals,
                   non_penalty_expected_goals, penalty_attempts
            FROM player_gameweek_attacking_decomposition ORDER BY fpl_id
            """
        ).fetchall()
    assert rows[0] == pytest.approx((1, 0.85, 0.79, 0.06, 1))
    assert rows[1] == pytest.approx((2, 0.30, 0.0, 0.30, 0))


def test_interval_evaluation_supports_named_high_risk_cohorts():
    result = evaluate_intervals(
        (
            ("premium", 5.0, 3.0, 7.0, 2.0),
            ("premium", 9.0, 3.0, 7.0, 2.0),
            ("role_change", 2.0, 0.0, 4.0, 3.0),
        )
    )
    by_cohort = {row.cohort: row for row in result}
    assert by_cohort["premium"].empirical_coverage == pytest.approx(0.5)
    assert by_cohort["role_change"].empirical_coverage == pytest.approx(1.0)

    cohorts = projection_cohorts(
        position="MID",
        price=10.5,
        data_quality_flags=(
            "OWN_TEAM_PROMOTED_PRIOR",
            "CURRENT_SEASON_APPEARANCE_ONLY",
            "POSITION_CHANGED_MID_TO_FWD",
        ),
    )
    assert {"premium", "promoted_team", "new_signing_or_current_only", "position_change"}.issubset(
        cohorts
    )

    calibration = evaluate_shadow_calibration(
        (
            ("premium", 5.0, 7.0, 6.0),
            ("premium", 5.0, 6.0, 5.5),
            ("cheap_enabler", 2.0, 2.0, 2.2),
        )
    )
    calibration_by_cohort = {row.cohort: row for row in calibration}
    assert calibration_by_cohort["premium"].mae_improvement > 0.0
    assert calibration_by_cohort["cheap_enabler"].mae_improvement < 0.0
