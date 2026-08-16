from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from fpl_model.model.appearance import MinutesScenario, project_appearance
from fpl_model.model.secondary import (
    BONUS_POINTS_MAXIMUM,
    SAVE_BUNDLE_SIZE,
    expected_poisson_bundles,
    poisson_tail_probability,
    project_benchwarmers_bonus,
    project_benchwarmers_defcon,
    project_benchwarmers_saves,
    project_discipline,
    weight_defcon,
    weight_linear_component,
    weight_saves,
)

REFERENCE_PATH = (
    Path(__file__).parents[1]
    / "docs"
    / "research"
    / "benchwarmers_saves_cards_bonus_defcon_reference.json"
)
REFERENCE = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))
REFERENCE_CASES = {case["label"]: case for case in REFERENCE["golden_cases"]}


@pytest.mark.parametrize(
    "label",
    ["high_save_goalkeeper", "low_save_elite_goalkeeper"],
)
def test_saves_projection_matches_spreadsheet_golden_cases(label: str):
    case = REFERENCE_CASES[label]
    inputs = case["inputs"]
    result = project_benchwarmers_saves(
        saves_per_90=inputs["MODEL!PS Saves/90"],
        opponent_xg_per_match=next(
            value
            for key, value in inputs.items()
            if key.startswith("PPts!R VS xG/90")
        ),
        league_average_xg_per_match=inputs["PPts!T LA xG/90"],
        position="GK",
    )

    assert result.workbook_xpts_if_start == pytest.approx(
        case["expected_outputs"]["saves_xpts_if_eligible"]
    )


def test_discrete_save_bundles_differ_from_continuous_workbook_rate():
    result = project_benchwarmers_saves(
        saves_per_90=3.628571428571428,
        opponent_xg_per_match=1.0,
        league_average_xg_per_match=1.0,
        position="GK",
    )

    assert result.workbook_xpts_if_start == pytest.approx(
        result.save_lambda_if_start / SAVE_BUNDLE_SIZE
    )
    assert result.exact_xpts_if_start == pytest.approx(
        expected_poisson_bundles(result.save_lambda_if_start, SAVE_BUNDLE_SIZE)
    )
    assert result.exact_xpts_if_start < result.workbook_xpts_if_start


def test_non_goalkeeper_save_component_is_zero():
    result = project_benchwarmers_saves(
        saves_per_90=4.0,
        opponent_xg_per_match=2.0,
        league_average_xg_per_match=1.0,
        position="DEF",
    )

    assert result.save_lambda_if_start == 0.0
    assert result.exact_xpts_if_start == 0.0


def test_cards_projection_matches_chiesa_and_restores_red_card_rate():
    case = REFERENCE_CASES["card_prone_and_rotation_prone_midfielder"]
    yellow_rate = case["inputs"]["MODEL!PS YC/St"]
    result = project_discipline(
        yellow_card_rate_if_start=yellow_rate,
        red_card_rate_if_start=0.02,
    )

    assert result.yellow_card_xpts_if_start == pytest.approx(
        case["expected_outputs"]["cards_xpts_if_eligible"]
    )
    assert result.red_card_xpts_if_start == pytest.approx(-0.06)
    assert result.workbook_red_card_xpts_if_start == 0.0


def test_bonus_projection_matches_haaland_golden_case():
    case = REFERENCE_CASES["strong_historical_bonus_forward"]
    inputs = case["inputs"]
    control = next(
        item
        for item in REFERENCE["control_assumptions"]
        if "B43" in item["cells"]
    )["cached_value"]
    result = project_benchwarmers_bonus(
        previous_starts=34,
        previous_bonus_per_start=inputs["MODEL!PS Bonus/Start"],
        previous_bps_per_start=inputs["MODEL!PS BPS/Start"],
        current_starts=0,
        current_bonus_per_start=0.0,
        current_bps_per_start=0.0,
        league_average_bonus_per_bps=control[
            "B45_Average_Bonus_per_BPS_per_Start"
        ],
        defensive_fixture_multiplier=1.0,
        attacking_fixture_multiplier=inputs[
            "PPts!W VS xGC/90/LA (used for FWD)"
        ],
        position="FWD",
    )

    assert result.workbook_uncapped_xpts_if_start == pytest.approx(
        case["expected_outputs"]["bonus_xpts_if_eligible"]
    )
    assert result.bounded_xpts_if_start == pytest.approx(
        case["expected_outputs"]["bonus_xpts_if_eligible"]
    )


def test_bonus_projection_uses_bps_for_small_samples_and_caps_output():
    result = project_benchwarmers_bonus(
        previous_starts=5,
        previous_bonus_per_start=3.0,
        previous_bps_per_start=200.0,
        current_starts=0,
        current_bonus_per_start=0.0,
        current_bps_per_start=0.0,
        league_average_bonus_per_bps=0.02,
        defensive_fixture_multiplier=2.0,
        attacking_fixture_multiplier=1.0,
        position="DEF",
    )

    assert result.historical_bonus_rate_if_start == pytest.approx(4.0)
    assert result.workbook_uncapped_xpts_if_start == pytest.approx(8.0)
    assert result.bounded_xpts_if_start == BONUS_POINTS_MAXIMUM


@pytest.mark.parametrize(
    ("label", "position", "expected_threshold"),
    [
        ("high_defcon_defender", "DEF", 10),
        ("high_defcon_midfielder", "MID", 12),
        ("promoted_team_missing_join_defender", "DEF", 10),
    ],
)
def test_defcon_projection_matches_spreadsheet_golden_cases(
    label: str,
    position: str,
    expected_threshold: int,
):
    case = REFERENCE_CASES[label]
    poisson_lambda = case["inputs"]["MODEL!LF DC/St"]
    result = project_benchwarmers_defcon(
        long_form_lambda_if_start=poisson_lambda,
        short_form_lambda_if_start=0.0,
        position=position,
    )

    assert result.threshold == expected_threshold
    assert result.xpts_if_start == pytest.approx(
        case["expected_outputs"]["defcon_xpts_if_eligible"]
    )


def test_poisson_tail_matches_extracted_defcon_probability():
    assert poisson_tail_probability(11.341545012165449, 10) == pytest.approx(
        1.390755886767129 / 2.0
    )
    assert poisson_tail_probability(14.759174311926605, 12) == pytest.approx(
        1.5975374509679097 / 2.0
    )


def test_weighted_components_use_start_and_substitute_not_absence():
    appearance = project_appearance(
        [
            MinutesScenario(0.50, 75.0, started=True),
            MinutesScenario(0.20, 15.0, started=False),
            MinutesScenario(0.30, 0.0, started=False),
        ]
    )
    linear = weight_linear_component(
        -1.0,
        appearance,
        substitute_to_start_minutes_ratio=0.2,
    )
    saves = weight_saves(
        project_benchwarmers_saves(
            saves_per_90=4.0,
            opponent_xg_per_match=1.0,
            league_average_xg_per_match=1.0,
            position="GK",
        ),
        appearance,
        substitute_to_start_minutes_ratio=0.2,
    )
    defcon = weight_defcon(
        project_benchwarmers_defcon(
            long_form_lambda_if_start=12.0,
            short_form_lambda_if_start=12.0,
            position="DEF",
        ),
        appearance,
        substitute_to_start_minutes_ratio=0.2,
        position="DEF",
    )

    assert linear.total_xpts == pytest.approx(-0.54)
    assert saves.total_xpts == pytest.approx(
        0.50 * expected_poisson_bundles(4.0, 3)
        + 0.20 * expected_poisson_bundles(0.8, 3)
    )
    assert defcon.total_xpts == pytest.approx(
        0.50 * 2.0 * poisson_tail_probability(12.0, 10)
        + 0.20 * 2.0 * poisson_tail_probability(2.4, 10)
    )


@pytest.mark.parametrize(
    ("poisson_lambda", "bundle_size"),
    [(-0.1, 3), (1.0, 0), (math.nan, 3)],
)
def test_poisson_bundle_expectation_rejects_invalid_inputs(
    poisson_lambda: float,
    bundle_size: int,
):
    with pytest.raises(ValueError):
        expected_poisson_bundles(poisson_lambda, bundle_size)


def test_component_functions_reject_unknown_positions():
    with pytest.raises(ValueError, match="position"):
        project_benchwarmers_saves(
            saves_per_90=1.0,
            opponent_xg_per_match=1.0,
            league_average_xg_per_match=1.0,
            position="UNKNOWN",
        )
    with pytest.raises(ValueError, match="position"):
        project_benchwarmers_defcon(
            long_form_lambda_if_start=1.0,
            short_form_lambda_if_start=1.0,
            position="UNKNOWN",
        )
