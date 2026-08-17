from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fpl_model.model.appearance import MinutesScenario, project_appearance
from fpl_model.model.attacking import (
    AttackingWindow,
    project_benchwarmers_attacking_rates,
    weight_attacking_rates,
)
from fpl_model.model.baseline import (
    BaselineComponentProjections,
    compose_baseline_projection,
)
from fpl_model.model.defence import (
    project_benchwarmers_defensive_rates,
    weight_defensive_rates,
)
from fpl_model.model.fixture import FixtureContext
from fpl_model.model.secondary import (
    project_benchwarmers_bonus,
    project_benchwarmers_defcon,
    project_benchwarmers_saves,
    project_discipline,
    weight_defcon,
    weight_linear_component,
    weight_saves,
)


def test_composes_all_components_and_applies_home_away_once():
    appearance = project_appearance(
        [
            MinutesScenario(0.7, 75.0, started=True),
            MinutesScenario(0.2, 15.0, started=False),
            MinutesScenario(0.1, 0.0, started=False),
        ]
    )
    attacking = weight_attacking_rates(
        project_benchwarmers_attacking_rates(
            AttackingWindow(minutes=900.0, expected_goals=2.0, expected_assists=1.5),
            AttackingWindow(minutes=450.0, expected_goals=1.5, expected_assists=1.0),
            minutes_per_start_fraction=0.8,
            position="GK",
            opponent_defensive_multiplier=1.1,
        ),
        appearance,
        substitute_to_start_minutes_ratio=0.2,
    )
    defensive = weight_defensive_rates(
        project_benchwarmers_defensive_rates(
            corrected_team_xgc_per_match=1.0,
            opponent_xg_per_match=1.2,
            league_average_xg_per_match=1.5,
            position="GK",
        ),
        appearance,
        substitute_to_start_minutes_ratio=0.2,
        position="GK",
    )
    saves = weight_saves(
        project_benchwarmers_saves(
            saves_per_90=3.5,
            opponent_xg_per_match=1.2,
            league_average_xg_per_match=1.5,
            position="GK",
        ),
        appearance,
        substitute_to_start_minutes_ratio=0.2,
    )
    discipline = project_discipline(
        yellow_card_rate_if_start=0.1,
        red_card_rate_if_start=0.01,
    )
    yellow_cards = weight_linear_component(
        discipline.yellow_card_xpts_if_start,
        appearance,
        substitute_to_start_minutes_ratio=0.2,
    )
    red_cards = weight_linear_component(
        discipline.red_card_xpts_if_start,
        appearance,
        substitute_to_start_minutes_ratio=0.2,
    )
    bonus_rate = project_benchwarmers_bonus(
        previous_starts=20,
        previous_bonus_per_start=0.3,
        previous_bps_per_start=15.0,
        current_starts=10,
        current_bonus_per_start=0.4,
        current_bps_per_start=16.0,
        league_average_bonus_per_bps=0.02,
        defensive_fixture_multiplier=1.1,
        attacking_fixture_multiplier=1.0,
        position="GK",
    )
    bonus = weight_linear_component(
        bonus_rate.bounded_xpts_if_start,
        appearance,
        substitute_to_start_minutes_ratio=0.2,
    )
    defcon = weight_defcon(
        project_benchwarmers_defcon(
            long_form_lambda_if_start=0.0,
            short_form_lambda_if_start=0.0,
            position="GK",
        ),
        appearance,
        substitute_to_start_minutes_ratio=0.2,
        position="GK",
    )
    fixture = FixtureContext(
        fixture_id=101,
        gameweek=1,
        slot=1,
        team_id=1,
        opponent_id=2,
        is_home=True,
        kickoff=datetime(2026, 8, 15, 14, tzinfo=UTC),
    )

    result = compose_baseline_projection(
        fixture,
        BaselineComponentProjections(
            appearance=appearance,
            attacking=attacking,
            defensive=defensive,
            saves=saves,
            yellow_cards=yellow_cards,
            red_cards=red_cards,
            bonus=bonus,
            defcon=defcon,
        ),
    )

    assert result.components.appearance == appearance.appearance_xpts
    assert result.components.goals == attacking.goal_xpts
    assert result.components.assists == attacking.assist_xpts
    assert result.components.clean_sheet == defensive.clean_sheet_xpts
    assert result.components.saves == saves.total_xpts
    assert result.components.red_cards < 0.0
    assert result.components.defcon == 0.0
    assert result.pre_home_away_xpts == pytest.approx(result.components.total_xpts)
    assert result.total_xpts == pytest.approx(result.pre_home_away_xpts * 1.05)
