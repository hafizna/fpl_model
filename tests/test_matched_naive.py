from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from fpl_model.validation.backtest import BacktestObservation, score_predictions
from fpl_model.validation.matched_naive import matched_naive_observations

DEADLINE_GW3 = datetime(2025, 8, 30, 12, 30, tzinfo=UTC)


def _observation(*, player_id: int, fixture_id: int, gameweek: int, deadline: datetime) -> BacktestObservation:
    kickoff = deadline + timedelta(minutes=90)
    return BacktestObservation(
        season="2025-26",
        gameweek=gameweek,
        deadline=deadline,
        fixture_kickoff=kickoff,
        feature_cutoff=deadline,
        outcome_available_at=kickoff + timedelta(hours=3),
        player_id=player_id,
        fixture_id=fixture_id,
        predicted_xpts=99.0,  # deliberately wrong; matched_naive must overwrite it
        actual_points=2.0,
    )


def _gameweeks_frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_matched_naive_predicts_expanding_mean_restricted_to_scored_keys():
    observations = (
        _observation(player_id=1001, fixture_id=300, gameweek=3, deadline=DEADLINE_GW3),
    )
    gameweeks = _gameweeks_frame(
        [
            {
                "code": 1001,
                "fixture": 100,
                "kickoff_time": "2025-08-16T14:00:00Z",
                "total_points": 4,
            },
            {
                "code": 1001,
                "fixture": 101,
                "kickoff_time": "2025-08-23T14:00:00Z",
                "total_points": 6,
            },
            # Not part of the scored set; must be ignored for player 1001's mean.
            {
                "code": 9999,
                "fixture": 102,
                "kickoff_time": "2025-08-23T14:00:00Z",
                "total_points": 100,
            },
        ]
    )

    matched = matched_naive_observations(observations, gameweeks)

    assert len(matched) == 1
    assert matched[0].predicted_xpts == pytest.approx((4 + 6) / 2)
    # actual_points/identity fields must be untouched.
    assert matched[0].actual_points == observations[0].actual_points
    assert matched[0].player_id == observations[0].player_id


def test_matched_naive_cold_starts_at_zero_with_no_prior_points():
    observations = (
        _observation(player_id=2002, fixture_id=300, gameweek=3, deadline=DEADLINE_GW3),
    )
    gameweeks = _gameweeks_frame(
        [
            {
                "code": 2002,
                "fixture": 400,
                "kickoff_time": "2025-09-10T14:00:00Z",  # after the deadline
                "total_points": 10,
            },
        ]
    )

    matched = matched_naive_observations(observations, gameweeks)

    assert matched[0].predicted_xpts == 0.0


def test_matched_naive_deduplicates_exact_duplicate_rows():
    # Regression test: an exact duplicate row in the raw source must not be
    # double-counted in the expanding mean (found in the live 2025-26 archive).
    # One row worth 4, its exact duplicate, and one distinct prior fixture
    # worth 8: with dedup the mean is (4 + 8) / 2 = 6.0; without dedup the
    # duplicate survives and the mean becomes (4 + 4 + 8) / 3 = 16/3 (~5.33),
    # so this genuinely fails if drop_duplicates() is removed -- unlike a
    # same-value duplicate, whose mean is unchanged either way.
    observations = (
        _observation(player_id=1001, fixture_id=300, gameweek=3, deadline=DEADLINE_GW3),
    )
    duplicated_row = {
        "code": 1001,
        "fixture": 100,
        "kickoff_time": "2025-08-16T14:00:00Z",
        "total_points": 4,
    }
    distinct_row = {
        "code": 1001,
        "fixture": 101,
        "kickoff_time": "2025-08-23T14:00:00Z",
        "total_points": 8,
    }
    gameweeks = _gameweeks_frame([duplicated_row, dict(duplicated_row), distinct_row])

    matched = matched_naive_observations(observations, gameweeks)

    assert matched[0].predicted_xpts == pytest.approx(6.0)


def test_matched_naive_respects_outcome_availability_gate():
    observations = (
        _observation(player_id=1001, fixture_id=300, gameweek=3, deadline=DEADLINE_GW3),
    )
    gameweeks = _gameweeks_frame(
        [
            {
                "code": 1001,
                "fixture": 100,
                "kickoff_time": "2025-08-16T14:00:00Z",
                "total_points": 4,
            },
            {
                # Kicks off before the deadline, but its outcome (kickoff + 3h)
                # is not available until after the deadline -- must be excluded.
                "code": 1001,
                "fixture": 101,
                "kickoff_time": "2025-08-30T10:00:00Z",
                "total_points": 100,
            },
        ]
    )

    matched = matched_naive_observations(observations, gameweeks)

    assert matched[0].predicted_xpts == pytest.approx(4.0)


def test_matched_naive_metrics_are_directly_comparable_via_score_predictions():
    observations = (
        _observation(player_id=1001, fixture_id=300, gameweek=3, deadline=DEADLINE_GW3),
        _observation(player_id=2002, fixture_id=301, gameweek=3, deadline=DEADLINE_GW3),
    )
    gameweeks = _gameweeks_frame(
        [
            {
                "code": 1001,
                "fixture": 100,
                "kickoff_time": "2025-08-16T14:00:00Z",
                "total_points": 4,
            },
        ]
    )

    matched = matched_naive_observations(observations, gameweeks)
    metrics = score_predictions(matched)

    assert metrics.observations == 2
