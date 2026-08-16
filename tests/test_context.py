from datetime import date, datetime, timedelta

import pytest

from fpl_model.context.congestion import PriorAppearance, workload_features
from fpl_model.context.readiness import TournamentReadiness
from fpl_model.schema.records import PreseasonAppearance


def test_preseason_appearance_validates_minutes():
    row = PreseasonAppearance(player_id=1, match_id="CHE_PRE_01", started=True, minutes=65)
    assert row.minutes == 65

    with pytest.raises(ValueError):
        PreseasonAppearance(player_id=1, match_id="CHE_PRE_01", started=True, minutes=150)


def test_tournament_readiness_is_descriptive():
    readiness = TournamentReadiness(
        tournament_minutes=620,
        last_tournament_match=date(2026, 7, 19),
        club_return_date=date(2026, 8, 3),
        preseason_minutes=45,
    )
    features = readiness.features(date(2026, 8, 20))
    assert features["days_since_last_tournament_match"] == 32
    assert features["training_days"] == 17
    assert features["preseason_minutes"] == 45.0


def test_workload_features_ignore_future_appearances():
    deadline = datetime(2026, 9, 12, 12, 0)
    appearances = [
        PriorAppearance(deadline - timedelta(days=3), 90),
        PriorAppearance(deadline - timedelta(days=8), 70),
        PriorAppearance(deadline + timedelta(days=1), 90),
    ]

    result = workload_features(appearances, deadline=deadline)
    assert result["minutes_last_7d"] == 90.0
    assert result["minutes_last_14d"] == 160.0
    assert result["matches_last_14d"] == 2
    assert result["rest_days"] == pytest.approx(3.0)
