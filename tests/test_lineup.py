from __future__ import annotations

from math import sqrt

import pytest

from fpl_model.decision.lineup import PlayerGameweekProjection, recommend_lineup
from tests.test_squad import _players, _validate


def _projections() -> list[PlayerGameweekProjection]:
    points = {
        1: 4.0,
        2: 5.0,
        3: 4.5,
        4: 4.0,
        5: 9.0,
        6: 6.0,
        7: 5.5,
        8: 5.0,
        9: 10.0,
        10: 7.0,
        11: 6.5,
        12: 2.0,
        13: 3.0,
        14: 2.5,
        15: 3.5,
    }
    return [
        PlayerGameweekProjection(fpl_id=fpl_id, expected_points=xpts, uncertainty=1.0)
        for fpl_id, xpts in points.items()
    ]


def test_recommends_maximum_xpts_legal_lineup_captain_and_bench_order():
    recommendation = recommend_lineup(_validate(_players()), _projections())

    assert [player.fpl_id for player in recommendation.starters] == list(range(1, 12))
    assert recommendation.formation == "3-4-3"
    assert recommendation.captain.fpl_id == 9
    assert recommendation.vice_captain.fpl_id == 5
    assert recommendation.bench_goalkeeper.fpl_id == 12
    assert [player.fpl_id for player in recommendation.outfield_bench_order] == [15, 13, 14]
    assert recommendation.starting_xpts == pytest.approx(66.5)
    assert recommendation.captain_bonus_xpts == pytest.approx(10.0)
    assert recommendation.total_xpts == pytest.approx(76.5)
    assert recommendation.uncertainty == pytest.approx(sqrt(14.0))


def test_formation_constraints_override_naive_top_eleven():
    projections = _projections()
    projections = [
        PlayerGameweekProjection(
            fpl_id=row.fpl_id,
            expected_points=(
                {9: -10.0, 10: -20.0, 11: -30.0}.get(
                    row.fpl_id,
                    20.0 if row.fpl_id in {13, 14, 15} else row.expected_points,
                )
            ),
            uncertainty=row.uncertainty,
        )
        for row in projections
    ]

    recommendation = recommend_lineup(_validate(_players()), projections)

    selected_forwards = {
        player.fpl_id for player in recommendation.starters if player.position == "FWD"
    }
    assert selected_forwards == {9}
    assert sum(player.position == "GK" for player in recommendation.starters) == 1
    assert sum(player.position == "DEF" for player in recommendation.starters) >= 3
    assert sum(player.position == "FWD" for player in recommendation.starters) >= 1


def test_rejects_missing_or_duplicate_projection_instead_of_assuming_zero():
    projections = _projections()
    with pytest.raises(ValueError, match=r"missing=\[15\]"):
        recommend_lineup(_validate(_players()), projections[:-1])

    with pytest.raises(ValueError, match="duplicate fpl_id"):
        recommend_lineup(_validate(_players()), [*projections, projections[0]])


def test_propagates_projection_and_squad_quality_flags():
    squad = _validate(_players())
    projections = _projections()
    projections[0] = PlayerGameweekProjection(
        fpl_id=1,
        expected_points=4.0,
        uncertainty=None,
        data_quality_flags=("LOW_RATE_COVERAGE",),
    )

    recommendation = recommend_lineup(squad, projections)

    assert recommendation.uncertainty is None
    assert recommendation.data_quality_flags == ("LOW_RATE_COVERAGE",)
