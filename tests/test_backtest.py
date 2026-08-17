from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fpl_model.validation.backtest import (
    BacktestObservation,
    score_predictions,
    walk_forward_folds,
)


def _observation(
    *,
    gameweek: int,
    deadline_day: int,
    kickoff_day: int,
    outcome_day: int,
    fixture_id: int,
    predicted: float = 4.0,
    actual: float = 5.0,
) -> BacktestObservation:
    return BacktestObservation(
        season="2026-27",
        gameweek=gameweek,
        deadline=datetime(2026, 8, deadline_day, 10, tzinfo=UTC),
        fixture_kickoff=datetime(2026, 8, kickoff_day, 14, tzinfo=UTC),
        feature_cutoff=datetime(2026, 8, deadline_day, 9, tzinfo=UTC),
        outcome_available_at=datetime(2026, 8, outcome_day, 18, tzinfo=UTC),
        player_id=100,
        fixture_id=fixture_id,
        predicted_xpts=predicted,
        actual_points=actual,
    )


def test_walk_forward_uses_only_outcomes_available_before_deadline():
    gw1 = _observation(
        gameweek=1,
        deadline_day=14,
        kickoff_day=15,
        outcome_day=15,
        fixture_id=1,
    )
    postponed_gw1 = _observation(
        gameweek=1,
        deadline_day=14,
        kickoff_day=20,
        outcome_day=25,
        fixture_id=2,
    )
    gw2 = _observation(
        gameweek=2,
        deadline_day=21,
        kickoff_day=22,
        outcome_day=22,
        fixture_id=3,
    )

    first, second = walk_forward_folds([gw2, postponed_gw1, gw1])

    assert first.training == ()
    assert set(first.evaluation) == {gw1, postponed_gw1}
    assert second.training == (gw1,)
    assert second.evaluation == (gw2,)


def test_minimum_training_rows_skips_early_folds():
    gw1 = _observation(
        gameweek=1,
        deadline_day=14,
        kickoff_day=15,
        outcome_day=15,
        fixture_id=1,
    )
    gw2 = _observation(
        gameweek=2,
        deadline_day=21,
        kickoff_day=22,
        outcome_day=22,
        fixture_id=2,
    )

    folds = walk_forward_folds(
        [gw1, gw2],
        minimum_training_observations=1,
    )

    assert len(folds) == 1
    assert folds[0].training == (gw1,)


def test_metrics_are_transparent_and_unweighted():
    first = _observation(
        gameweek=1,
        deadline_day=14,
        kickoff_day=15,
        outcome_day=15,
        fixture_id=1,
        predicted=4.0,
        actual=6.0,
    )
    second = _observation(
        gameweek=1,
        deadline_day=14,
        kickoff_day=15,
        outcome_day=15,
        fixture_id=2,
        predicted=3.0,
        actual=2.0,
    )

    metrics = score_predictions([first, second])

    assert metrics.observations == 2
    assert metrics.mean_predicted_xpts == pytest.approx(3.5)
    assert metrics.mean_actual_points == pytest.approx(4.0)
    assert metrics.mean_error == pytest.approx(-0.5)
    assert metrics.mean_absolute_error == pytest.approx(1.5)
    assert metrics.root_mean_squared_error == pytest.approx((2.5) ** 0.5)


def test_rejects_feature_cutoff_after_deadline():
    with pytest.raises(ValueError, match="feature_cutoff"):
        BacktestObservation(
            season="2026-27",
            gameweek=1,
            deadline=datetime(2026, 8, 14, 10, tzinfo=UTC),
            fixture_kickoff=datetime(2026, 8, 15, 14, tzinfo=UTC),
            feature_cutoff=datetime(2026, 8, 14, 11, tzinfo=UTC),
            outcome_available_at=datetime(2026, 8, 15, 18, tzinfo=UTC),
            player_id=1,
            fixture_id=1,
            predicted_xpts=4.0,
            actual_points=5.0,
        )


def test_rejects_naive_timestamps_and_duplicate_predictions():
    valid = _observation(
        gameweek=1,
        deadline_day=14,
        kickoff_day=15,
        outcome_day=15,
        fixture_id=1,
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        BacktestObservation(
            season="2026-27",
            gameweek=1,
            deadline=datetime(2026, 8, 14, 10),
            fixture_kickoff=datetime(2026, 8, 15, 14, tzinfo=UTC),
            feature_cutoff=datetime(2026, 8, 14, 9, tzinfo=UTC),
            outcome_available_at=datetime(2026, 8, 15, 18, tzinfo=UTC),
            player_id=1,
            fixture_id=1,
            predicted_xpts=4.0,
            actual_points=5.0,
        )
    with pytest.raises(ValueError, match="duplicate"):
        walk_forward_folds([valid, valid])


def test_empty_metric_sample_is_rejected():
    with pytest.raises(ValueError, match="at least one"):
        score_predictions([])
