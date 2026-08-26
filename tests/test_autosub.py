from __future__ import annotations

import pytest

from fpl_model.decision.autosub import compute_expected_autosub_value
from fpl_model.decision.lineup import PlayerGameweekProjection, recommend_lineup
from tests.test_lineup import _projections
from tests.test_squad import _players, _validate


def _projections_with_appearance(
    overrides: dict[int, float] | None = None,
) -> dict[int, PlayerGameweekProjection]:
    """The standard _projections() fixture, with appearance_probability set to
    1.0 (certain to play) for every player except the given overrides."""
    overrides = overrides or {}
    base = {row.fpl_id: row for row in _projections()}
    result = {}
    for fpl_id, row in base.items():
        result[fpl_id] = PlayerGameweekProjection(
            fpl_id=row.fpl_id,
            expected_points=row.expected_points,
            uncertainty=row.uncertainty,
            appearance_probability=overrides.get(fpl_id, 1.0),
        )
    return result


def test_no_expected_value_when_every_player_is_certain_to_play():
    recommendation = recommend_lineup(_validate(_players()), _projections())
    projections = _projections_with_appearance()

    result = compute_expected_autosub_value(recommendation, projections)

    assert result.total_expected_bench_value == pytest.approx(0.0)
    assert result.bench_goalkeeper.expected_value == pytest.approx(0.0)
    assert all(row.expected_value == pytest.approx(0.0) for row in result.outfield_bench)


def test_goalkeeper_autosub_is_independent_of_outfield_and_uses_bench_gk_only():
    recommendation = recommend_lineup(_validate(_players()), _projections())
    # Starting GK (fpl_id=1) always blanks; bench GK (fpl_id=12) always plays.
    projections = _projections_with_appearance({1: 0.0, 12: 1.0})

    result = compute_expected_autosub_value(recommendation, projections)

    assert result.bench_goalkeeper.fpl_id == 12
    assert result.bench_goalkeeper.expected_value == pytest.approx(2.0)  # GK12's own xpts
    assert result.bench_goalkeeper.usage_probability == pytest.approx(1.0)
    assert all(row.expected_value == pytest.approx(0.0) for row in result.outfield_bench)


def test_goalkeeper_not_replaced_when_bench_goalkeeper_also_blanks():
    recommendation = recommend_lineup(_validate(_players()), _projections())
    projections = _projections_with_appearance({1: 1.0, 12: 0.0})

    result = compute_expected_autosub_value(recommendation, projections)

    assert result.bench_goalkeeper.expected_value == pytest.approx(0.0)
    assert result.bench_goalkeeper.usage_probability == pytest.approx(0.0)


def test_outfield_autosub_follows_bench_order_for_a_single_blank():
    recommendation = recommend_lineup(_validate(_players()), _projections())
    # fpl_id=11 (a FWD starter) always blanks. Bench order is [15(MID), 13(DEF), 14(DEF)].
    # Replacing a FWD with MID 15 keeps >=1 FWD (10, 9 remain) -- legal.
    projections = _projections_with_appearance({11: 0.0})

    result = compute_expected_autosub_value(recommendation, projections)

    bench_by_id = {row.fpl_id: row for row in result.outfield_bench}
    assert bench_by_id[15].expected_value == pytest.approx(3.5)  # player 15's own xpts
    assert bench_by_id[15].usage_probability == pytest.approx(1.0)
    assert bench_by_id[13].expected_value == pytest.approx(0.0)
    assert bench_by_id[14].expected_value == pytest.approx(0.0)


def test_outfield_autosub_skips_a_substitution_that_would_break_formation():
    recommendation = recommend_lineup(_validate(_players()), _projections())
    # All three FWD starters (9, 10, 11) blank. Bench order [15(MID), 13(DEF),
    # 14(DEF)] has no FWD, so subbing all three in would leave 0 FWD --
    # illegal. Only substitutions that KEEP >=1 FWD after each step apply:
    # the first two FWD blanks can be replaced (11->15, 10->13, leaving
    # FWD={9}, DEF={2,3,4,13}, MID={5,6,7,8}=11 players, legal each step);
    # the third (9) cannot be replaced by 14(DEF) since that would drop FWD to 0.
    projections = _projections_with_appearance({9: 0.0, 10: 0.0, 11: 0.0})

    result = compute_expected_autosub_value(recommendation, projections)

    bench_by_id = {row.fpl_id: row for row in result.outfield_bench}
    assert bench_by_id[15].usage_probability == pytest.approx(1.0)
    assert bench_by_id[13].usage_probability == pytest.approx(1.0)
    assert bench_by_id[14].usage_probability == pytest.approx(0.0)
    assert bench_by_id[14].expected_value == pytest.approx(0.0)


def test_expected_value_weights_by_blank_probability_when_uncertain():
    recommendation = recommend_lineup(_validate(_players()), _projections())
    # fpl_id=11 blanks with 40% probability.
    projections = _projections_with_appearance({11: 0.6})

    result = compute_expected_autosub_value(recommendation, projections)

    bench_by_id = {row.fpl_id: row for row in result.outfield_bench}
    assert bench_by_id[15].expected_value == pytest.approx(0.4 * 3.5)
    assert bench_by_id[15].usage_probability == pytest.approx(0.4)


def test_missing_appearance_probability_is_treated_as_certain_to_play():
    recommendation = recommend_lineup(_validate(_players()), _projections())
    # No appearance_probability override for fpl_id=11 (defaults to None via
    # the plain _projections() fixture, not _projections_with_appearance()).
    projections = {row.fpl_id: row for row in _projections()}

    result = compute_expected_autosub_value(recommendation, projections)

    assert result.total_expected_bench_value == pytest.approx(0.0)


def test_a_bench_player_is_used_for_at_most_one_substitution():
    recommendation = recommend_lineup(_validate(_players()), _projections())
    # Two FWD starters blank (10 and 11); bench MID 15 can only cover one.
    projections = _projections_with_appearance({10: 0.0, 11: 0.0})

    result = compute_expected_autosub_value(recommendation, projections)

    bench_by_id = {row.fpl_id: row for row in result.outfield_bench}
    # 15 covers the first blank found in starter order (10 comes before 11
    # among starters), 13 (DEF) covers the second -- both legal since FWD=9
    # remains after each step.
    assert bench_by_id[15].usage_probability == pytest.approx(1.0)
    assert bench_by_id[13].usage_probability == pytest.approx(1.0)
    assert bench_by_id[14].usage_probability == pytest.approx(0.0)


def test_total_expected_bench_value_sums_every_slot():
    recommendation = recommend_lineup(_validate(_players()), _projections())
    projections = _projections_with_appearance({1: 0.0, 12: 1.0, 11: 0.0})

    result = compute_expected_autosub_value(recommendation, projections)

    expected_total = result.bench_goalkeeper.expected_value + sum(
        row.expected_value for row in result.outfield_bench
    )
    assert result.total_expected_bench_value == pytest.approx(expected_total)
    assert result.total_expected_bench_value == pytest.approx(2.0 + 3.5)
