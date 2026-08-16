from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from fpl_model.model.appearance import MinutesScenario, project_appearance
from fpl_model.model.defence import (
    CLEAN_SHEET_POINTS_BY_POSITION,
    GOALS_CONCEDED_PENALTY_BY_POSITION,
    DefensiveWindow,
    corrected_team_xgc_per_match,
    expected_goals_conceded_pairs,
    project_benchwarmers_defensive_rates,
    weight_defensive_rates,
)

REFERENCE_PATH = (
    Path(__file__).parents[1]
    / "docs"
    / "research"
    / "benchwarmers_clean_sheets_goals_conceded_reference.json"
)
REFERENCE_CASES = {
    case["label"]: case
    for case in json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))["golden_cases"]
}


def _property_with_prefix(values: dict[str, object], prefix: str) -> float:
    return float(next(value for key, value in values.items() if key.startswith(prefix)))


def _rate_projection(label: str):
    case = REFERENCE_CASES[label]
    inputs = case["inputs"]
    return project_benchwarmers_defensive_rates(
        corrected_team_xgc_per_match=_property_with_prefix(
            inputs,
            "TABLES!LFxSF US xGC FIX",
        ),
        opponent_xg_per_match=_property_with_prefix(inputs, "PPts!R VS xG/90"),
        league_average_xg_per_match=_property_with_prefix(
            inputs,
            "PPts!T LA xG/90",
        ),
        position=case["position"],
    )


@pytest.mark.parametrize("label", list(REFERENCE_CASES))
def test_defensive_rate_projection_matches_spreadsheet_golden_cases(label: str):
    result = _rate_projection(label)
    case = REFERENCE_CASES[label]
    expected = case["expected_outputs"]
    cached = case["cached_intermediate_values"]

    assert result.poisson_lambda == pytest.approx(
        _property_with_prefix(cached, "PPts!AL")
    )
    assert result.clean_sheet_probability == pytest.approx(
        _property_with_prefix(cached, "PPts!AM")
    )
    assert result.clean_sheet_xpts_if_eligible == pytest.approx(
        expected["clean_sheet_xpts_if_eligible"]
    )
    assert result.workbook_goals_conceded_xpts_if_eligible == pytest.approx(
        expected["goals_conceded_xpts_if_eligible"]
    )


def test_promoted_team_correction_matches_spreadsheet_reference():
    case = REFERENCE_CASES["promoted_team_defender"]
    inputs = case["inputs"]
    raw_long = _property_with_prefix(inputs, "TABLES!LF xGC/90")
    raw_short = _property_with_prefix(inputs, "TABLES!SF xGC/90")

    corrected = corrected_team_xgc_per_match(
        DefensiveWindow(raw_long, 1.0),
        DefensiveWindow(raw_short, 1.0),
    )

    assert raw_long == raw_short
    assert corrected == pytest.approx(
        _property_with_prefix(inputs, "TABLES!LFxSF US xGC FIX")
    )


@pytest.mark.parametrize("position", CLEAN_SHEET_POINTS_BY_POSITION)
def test_positional_scoring_is_applied_after_shared_probabilities(position: str):
    result = project_benchwarmers_defensive_rates(
        corrected_team_xgc_per_match=1.0,
        opponent_xg_per_match=1.0,
        league_average_xg_per_match=1.0,
        position=position,
    )

    assert result.clean_sheet_probability == pytest.approx(math.exp(-1.0))
    assert result.clean_sheet_xpts_if_eligible == pytest.approx(
        CLEAN_SHEET_POINTS_BY_POSITION[position] * math.exp(-1.0)
    )
    assert result.workbook_goals_conceded_xpts_if_eligible == pytest.approx(
        GOALS_CONCEDED_PENALTY_BY_POSITION[position]
        * (1.0 - math.exp(-1.0) * 2.0)
    )


def test_exact_goals_conceded_penalty_includes_four_plus_goals():
    result = project_benchwarmers_defensive_rates(
        corrected_team_xgc_per_match=2.0,
        opponent_xg_per_match=1.0,
        league_average_xg_per_match=1.0,
        position="DEF",
    )

    assert result.expected_goals_conceded_pairs == pytest.approx(
        expected_goals_conceded_pairs(2.0)
    )
    assert result.exact_goals_conceded_xpts_if_full_match < (
        result.workbook_goals_conceded_xpts_if_eligible
    )


def test_defensive_exposure_uses_sixty_minutes_and_excludes_absences():
    rates = _rate_projection("rotation_prone_defender")
    appearance = project_appearance(
        [
            MinutesScenario(0.40, 75.0, started=True),
            MinutesScenario(0.30, 20.0, started=False),
            MinutesScenario(0.30, 0.0, started=False),
        ]
    )

    result = weight_defensive_rates(
        rates,
        appearance,
        substitute_to_start_minutes_ratio=20.0 / 75.0,
        position="DEF",
    )

    expected_clean_sheet_xpts = rates.clean_sheet_xpts_if_eligible * 0.40
    expected_goals_conceded_xpts = -(
        0.40 * expected_goals_conceded_pairs(rates.poisson_lambda)
        + 0.30
        * expected_goals_conceded_pairs(rates.poisson_lambda * 20.0 / 75.0)
    )
    assert result.clean_sheet_xpts == pytest.approx(expected_clean_sheet_xpts)
    assert result.goals_conceded_xpts == pytest.approx(
        expected_goals_conceded_xpts
    )
    assert result.total_xpts == pytest.approx(
        expected_clean_sheet_xpts + expected_goals_conceded_xpts
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"expected_goals_conceded": -0.1, "matches_played": 1.0},
        {"expected_goals_conceded": math.nan, "matches_played": 1.0},
        {"expected_goals_conceded": 1.0, "matches_played": 0.0},
        {"expected_goals_conceded": 0.0, "matches_played": math.inf},
    ],
)
def test_defensive_window_rejects_invalid_values(kwargs: dict[str, float]):
    with pytest.raises(ValueError):
        DefensiveWindow(**kwargs)


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("corrected_team_xgc_per_match", -0.1),
        ("opponent_xg_per_match", math.nan),
        ("league_average_xg_per_match", 0.0),
    ],
)
def test_defensive_rate_projection_rejects_invalid_parameters(
    argument: str,
    value: float,
):
    kwargs = {
        "corrected_team_xgc_per_match": 1.0,
        "opponent_xg_per_match": 1.0,
        "league_average_xg_per_match": 1.0,
        "position": "DEF",
        argument: value,
    }
    with pytest.raises(ValueError):
        project_benchwarmers_defensive_rates(**kwargs)


def test_defensive_rate_projection_rejects_unknown_position():
    with pytest.raises(ValueError, match="position"):
        project_benchwarmers_defensive_rates(
            corrected_team_xgc_per_match=1.0,
            opponent_xg_per_match=1.0,
            league_average_xg_per_match=1.0,
            position="UNKNOWN",
        )
