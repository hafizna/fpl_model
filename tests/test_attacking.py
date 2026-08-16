from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from fpl_model.model.appearance import (
    SeasonAppearanceHistory,
    project_benchwarmers_appearance,
)
from fpl_model.model.attacking import (
    GOAL_POINTS_BY_POSITION,
    AttackingWindow,
    project_benchwarmers_attacking_rates,
    weight_attacking_rates,
)

REFERENCE_PATH = (
    Path(__file__).parents[1]
    / "docs"
    / "research"
    / "benchwarmers_goals_assists_reference.json"
)
REFERENCE_CASES = {
    case["label"]: case
    for case in json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))["golden_cases"]
}


def _property_with_prefix(values: dict[str, float], prefix: str) -> float:
    return next(value for key, value in values.items() if key.startswith(prefix))


def _minutes_per_start_fraction(case: dict[str, object]) -> float:
    inputs = case["inputs"]
    assert isinstance(inputs, dict)
    if "MODEL!PS Mn/St %" in inputs:
        return float(inputs["MODEL!PS Mn/St %"])
    if "MODEL!PS Mn/St" in inputs:
        return float(inputs["MODEL!PS Mn/St"]) / 90.0
    if "MODEL!G+A PSxTS Mn/Start" in inputs:
        return float(inputs["MODEL!G+A PSxTS Mn/Start"])

    long_form_minutes = float(inputs["MODEL!LF G+A Mins"])
    long_form_xg = float(inputs["MODEL!LF xG"])
    if long_form_minutes == 0.0 or long_form_xg == 0.0:
        return 0.0

    cached = case["cached_intermediate_values"]
    assert isinstance(cached, dict)
    long_form_xg_per_start = _property_with_prefix(cached, "MODEL!LF xG/St")
    return long_form_xg_per_start / (long_form_xg / long_form_minutes * 90.0)


def _rate_projection(label: str):
    case = REFERENCE_CASES[label]
    inputs = case["inputs"]
    assert isinstance(inputs, dict)
    return project_benchwarmers_attacking_rates(
        AttackingWindow(
            minutes=float(inputs["MODEL!LF G+A Mins"]),
            expected_goals=float(inputs["MODEL!LF xG"]),
            expected_assists=float(inputs["MODEL!LF xA"]),
        ),
        AttackingWindow(
            minutes=float(inputs["MODEL!SF Mins"]),
            expected_goals=float(inputs["MODEL!SF xG"]),
            expected_assists=float(inputs["MODEL!SF xA"]),
        ),
        minutes_per_start_fraction=_minutes_per_start_fraction(case),
        position=str(case["position"]),
        opponent_defensive_multiplier=float(inputs["PPts!VS xGC/90/LA"]),
    )


@pytest.mark.parametrize("label", list(REFERENCE_CASES))
def test_attacking_rate_projection_matches_spreadsheet_golden_cases(label: str):
    result = _rate_projection(label)
    expected = REFERENCE_CASES[label]["expected_outputs"]

    assert result.goal_xpts_if_start == pytest.approx(expected["goal_xpts_if_start"])
    assert result.assist_xpts_if_start == pytest.approx(
        expected["assist_xpts_if_start"]
    )


def test_haaland_window_rates_match_cached_intermediates():
    result = _rate_projection("elite_high_xg_forward")
    cached = REFERENCE_CASES["elite_high_xg_forward"]["cached_intermediate_values"]

    assert result.long_form_xg_per_start == pytest.approx(
        _property_with_prefix(cached, "MODEL!LF xG/St")
    )
    assert result.short_form_xg_per_start == pytest.approx(
        _property_with_prefix(cached, "MODEL!SF xG/St")
    )
    assert result.long_form_xa_per_start == pytest.approx(
        _property_with_prefix(cached, "MODEL!LF xA/St")
    )
    assert result.short_form_xa_per_start == pytest.approx(
        _property_with_prefix(cached, "MODEL!SF xA/St")
    )


@pytest.mark.parametrize("position", GOAL_POINTS_BY_POSITION)
def test_goal_points_follow_fpl_position_scoring(position: str):
    result = project_benchwarmers_attacking_rates(
        AttackingWindow(90.0, expected_goals=1.0, expected_assists=0.0),
        AttackingWindow(90.0, expected_goals=1.0, expected_assists=0.0),
        minutes_per_start_fraction=1.0,
        position=position,
        opponent_defensive_multiplier=1.0,
    )

    assert result.goal_xpts_if_start == GOAL_POINTS_BY_POSITION[position]


def test_assist_boost_is_explicit_and_configurable():
    inputs = dict(
        long_form=AttackingWindow(90.0, 0.0, 1.0),
        short_form=AttackingWindow(90.0, 0.0, 1.0),
        minutes_per_start_fraction=1.0,
        position="MID",
        opponent_defensive_multiplier=1.0,
    )

    without_boost = project_benchwarmers_attacking_rates(**inputs, assist_boost=0.0)
    workbook_boost = project_benchwarmers_attacking_rates(**inputs, assist_boost=0.4)

    assert without_boost.assist_xpts_if_start == pytest.approx(3.0)
    assert workbook_boost.assist_xpts_if_start == pytest.approx(4.2)


def test_rotation_exposure_uses_substitute_appearance_not_every_non_start():
    rate_projection = _rate_projection("rotation_prone_substitute")
    appearance = project_benchwarmers_appearance(
        SeasonAppearanceHistory(
            1,
            25,
            10,
            minutes_per_start=60.0,
            minutes_per_substitute=11.0,
        ),
        SeasonAppearanceHistory(0, 0, 0),
    )

    result = weight_attacking_rates(
        rate_projection,
        appearance,
        substitute_to_start_minutes_ratio=11.0 / 60.0,
    )

    expected_exposure = 1.0 / 36.0 + (0.715 - 1.0 / 36.0) * (11.0 / 60.0)
    workbook_non_start_exposure = 1.0 / 36.0 + (1.0 - 1.0 / 36.0) * (
        11.0 / 60.0
    )
    assert result.attacking_exposure == pytest.approx(expected_exposure)
    assert result.attacking_exposure < workbook_non_start_exposure
    assert result.goal_xpts == pytest.approx(
        rate_projection.goal_xpts_if_start * expected_exposure
    )
    assert result.assist_xpts == pytest.approx(
        rate_projection.assist_xpts_if_start * expected_exposure
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"minutes": -1.0, "expected_goals": 0.0, "expected_assists": 0.0},
        {"minutes": 90.0, "expected_goals": math.nan, "expected_assists": 0.0},
        {"minutes": 90.0, "expected_goals": 0.0, "expected_assists": -0.1},
        {"minutes": 0.0, "expected_goals": 0.1, "expected_assists": 0.0},
    ],
)
def test_attacking_window_rejects_invalid_values(kwargs: dict[str, float]):
    with pytest.raises(ValueError):
        AttackingWindow(**kwargs)


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("minutes_per_start_fraction", -0.1),
        ("minutes_per_start_fraction", math.nan),
        ("long_form_weight", 1.1),
        ("opponent_defensive_multiplier", -1.0),
        ("assist_boost", math.inf),
    ],
)
def test_rate_projection_rejects_invalid_parameters(argument: str, value: float):
    kwargs = {
        "minutes_per_start_fraction": 1.0,
        "position": "MID",
        "opponent_defensive_multiplier": 1.0,
        argument: value,
    }
    with pytest.raises(ValueError):
        project_benchwarmers_attacking_rates(
            AttackingWindow(90.0, 0.1, 0.1),
            AttackingWindow(90.0, 0.1, 0.1),
            **kwargs,
        )


def test_rate_projection_rejects_unknown_position():
    with pytest.raises(ValueError, match="position"):
        project_benchwarmers_attacking_rates(
            AttackingWindow(90.0, 0.1, 0.1),
            AttackingWindow(90.0, 0.1, 0.1),
            minutes_per_start_fraction=1.0,
            position="UNKNOWN",
            opponent_defensive_multiplier=1.0,
        )
