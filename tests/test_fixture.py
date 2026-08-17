from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from fpl_model.model.fixture import (
    AWAY_XPTS_MULTIPLIER,
    HOME_XPTS_MULTIPLIER,
    FixtureStrength,
    ScoringComponents,
    aggregate_fixture_xpts_by_gameweek,
    apply_home_away_once,
    fixture_contexts_for_team,
    project_workbook_fixture_totals,
)

REFERENCE_PATH = (
    Path(__file__).parents[1]
    / "docs"
    / "research"
    / "benchwarmers_fixture_home_away_reference.json"
)
REFERENCE = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))
REFERENCE_CASES = {case["label"]: case for case in REFERENCE["golden_cases"]}


def _components_from_workbook_totals(
    *,
    pre_home_away_total: float,
    cameo_rate_total: float,
) -> ScoringComponents:
    return ScoringComponents(
        appearance=1.0,
        sixty_minutes=pre_home_away_total - 1.0 - cameo_rate_total,
        goals=cameo_rate_total,
    )


def test_fixture_strength_matches_raya_workbook_signals():
    case = REFERENCE_CASES["home_fixture_certain_starter"]
    inputs = case["raw_fixture_strength_inputs"]
    strength = FixtureStrength(
        opponent_xg_per_match=inputs["VS_xG_90"],
        opponent_xgc_per_match=inputs["VS_xGC_90"],
        league_average_xg_per_match=inputs["LA_xG_90"],
        league_average_xgc_per_match=inputs["LA_xGC_90"],
    )

    assert strength.opponent_attack_ratio == pytest.approx(
        case["fixture_multipliers"]["saves_multiplier_M_3xSaves90X"]
    )
    assert strength.workbook_defensive_bonus_multiplier == pytest.approx(
        case["fixture_multipliers"][
            "VS_xG_90_LA_saves_and_bonus_GK_DEF"
        ]
    )
    assert strength.opponent_defensive_weakness_ratio == pytest.approx(
        case["fixture_multipliers"][
            "VS_xGC_90_LA_goals_assists_CS_bonus_MID_FWD"
        ]
    )


def test_workbook_home_certain_starter_matches_raya_golden_case():
    case = REFERENCE_CASES["home_fixture_certain_starter"]
    pre_total = case["component_totals_before_home_away"]["PRE_H_A_TOTAL_AY"]
    projection = project_workbook_fixture_totals(
        _components_from_workbook_totals(
            pre_home_away_total=pre_total,
            cameo_rate_total=0.0,
        ),
        is_home=True,
        t1_appearance_xpts=case["appearance_start_inputs"]["T1"],
        start_probability=case["appearance_start_inputs"]["Start_pct_BH"],
        substitute_to_start_minutes_ratio=0.0,
    )

    assert projection.home_away_multiplier == HOME_XPTS_MULTIPLIER
    assert projection.if_start_total_xpts == pytest.approx(
        case["final_values"]["BA_IF_START_TOTAL"]
    )
    assert projection.workbook_true_total_xpts == pytest.approx(
        case["final_values"]["BI_TRUE_TOTAL"]
    )
    assert projection.double_application_delta == 0.0


def test_workbook_away_rotation_case_proves_double_application():
    case = REFERENCE_CASES["away_fixture_rotation_prone"]
    pre_total = case["component_totals_before_home_away"]["PRE_H_A_TOTAL_AY"]
    cameo_rate_total = 0.5441743893341721
    projection = project_workbook_fixture_totals(
        _components_from_workbook_totals(
            pre_home_away_total=pre_total,
            cameo_rate_total=cameo_rate_total,
        ),
        is_home=False,
        t1_appearance_xpts=case["appearance_start_inputs"]["T1"],
        start_probability=case["appearance_start_inputs"]["Start_pct_BH"],
        substitute_to_start_minutes_ratio=case["appearance_start_inputs"][
            "PSxTS_Mn_Sub_Mn_St_BF"
        ],
    )

    assert projection.home_away_multiplier == AWAY_XPTS_MULTIPLIER
    assert projection.if_not_start_total_xpts == pytest.approx(
        case["final_values"]["BG_IF_NOT_START_TOTAL"]
    )
    assert projection.workbook_true_total_xpts == pytest.approx(
        case["final_values"]["BI_TRUE_TOTAL"]
    )
    assert projection.single_application_true_total_xpts == pytest.approx(
        2.6857843150327287
    )
    assert projection.double_application_delta == pytest.approx(
        -0.0032374250914575242
    )


def test_manual_start_override_changes_magnitude_not_mechanism():
    case = REFERENCE_CASES["manual_start_override_home_fixture"]
    pre_total = case["component_totals_before_home_away"]["PRE_H_A_TOTAL_AY"]
    projection = project_workbook_fixture_totals(
        _components_from_workbook_totals(
            pre_home_away_total=pre_total,
            cameo_rate_total=3.452068407311668,
        ),
        is_home=True,
        t1_appearance_xpts=case["appearance_start_inputs"]["T1_BE"],
        start_probability=case["appearance_start_inputs"]["Start_pct_BH"],
        substitute_to_start_minutes_ratio=case["appearance_start_inputs"][
            "PSxTS_Mn_Sub_Mn_St_BF"
        ],
    )

    assert projection.if_not_start_total_xpts == pytest.approx(
        case["final_values"]["BG_IF_NOT_START_TOTAL"]
    )
    assert projection.workbook_true_total_xpts == pytest.approx(
        case["final_values"]["BI_TRUE_TOTAL"]
    )
    assert projection.single_application_true_total_xpts == pytest.approx(
        4.651024962420566
    )
    assert projection.double_application_delta == pytest.approx(
        0.023869790084702913
    )


def test_blank_fixture_has_no_final_projection_but_retains_workbook_bg_quirk():
    projection = project_workbook_fixture_totals(
        ScoringComponents(goals=1.0),
        is_home=None,
        t1_appearance_xpts=0.9,
        start_probability=0.5,
        substitute_to_start_minutes_ratio=0.2,
    )

    assert projection.has_fixture is False
    assert projection.if_start_total_xpts == 0.0
    assert projection.if_not_start_total_xpts == pytest.approx(0.19)
    assert projection.workbook_true_total_xpts == 0.0
    assert projection.single_application_true_total_xpts == 0.0


def test_coherent_projection_applies_home_away_once():
    home = apply_home_away_once(5.0, is_home=True)
    away = apply_home_away_once(5.0, is_home=False)

    assert home.total_xpts == pytest.approx(5.25)
    assert away.total_xpts == pytest.approx(4.75)
    assert home.total_xpts / away.total_xpts == pytest.approx(1.05 / 0.95)


def test_fixture_contexts_keep_dgw_rows_and_do_not_create_blank_slots():
    fixtures = [
        {
            "id": 10,
            "event": 1,
            "team_h": 12,
            "team_a": 5,
            "kickoff_time": "2026-08-22T14:00:00Z",
        },
        {
            "id": 11,
            "event": 2,
            "team_h": 8,
            "team_a": 12,
            "kickoff_time": "2026-08-29T14:00:00Z",
        },
        {
            "id": 369,
            "event": 2,
            "team_h": 12,
            "team_a": 18,
            "kickoff_time": "2027-05-23T14:00:00Z",
        },
        {
            "id": 12,
            "event": None,
            "team_h": 12,
            "team_a": 3,
            "kickoff_time": None,
        },
    ]

    contexts = fixture_contexts_for_team(fixtures, team_id=12)

    assert [(item.gameweek, item.slot) for item in contexts] == [(1, 1), (2, 1), (2, 2)]
    assert contexts[0].is_home is True
    assert contexts[1].is_home is False
    assert contexts[2].opponent_id == 18
    assert all(item.gameweek != 37 for item in contexts)


def test_dgw_aggregation_sums_independent_fixture_projections():
    fixtures = [
        {"id": 1, "event": 2, "team_h": 1, "team_a": 2},
        {"id": 2, "event": 2, "team_h": 3, "team_a": 1},
    ]
    first, second = fixture_contexts_for_team(fixtures, team_id=1)

    totals = aggregate_fixture_xpts_by_gameweek(
        [(first, 2.2264326064593503), (second, 2.8900205698773322)]
    )

    assert totals == {2: pytest.approx(5.116453176336682)}


@pytest.mark.parametrize(
    "value",
    [math.nan, math.inf],
)
def test_home_away_projection_rejects_non_finite_xpts(value: float):
    with pytest.raises(ValueError):
        apply_home_away_once(value, is_home=True)


def test_fixture_strength_rejects_invalid_league_average():
    with pytest.raises(ValueError, match="league_average_xg_per_match"):
        FixtureStrength(
            opponent_xg_per_match=1.0,
            opponent_xgc_per_match=1.0,
            league_average_xg_per_match=0.0,
            league_average_xgc_per_match=1.0,
        )
