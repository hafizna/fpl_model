from __future__ import annotations

from fpl_model.decision.role_scenario_sensitivity import (
    evaluate_role_scenario_sensitivity,
)
from fpl_model.validation.role_state import LIKELY_STARTER, ROTATION, RoleStateResult
from tests.test_lineup import _projections
from tests.test_squad import _players, _validate

_STABLE_REASON = "test fixture"


def _role_states(rotation_ids: set[int]) -> dict[int, RoleStateResult]:
    return {
        fpl_id: RoleStateResult(
            role_state=ROTATION if fpl_id in rotation_ids else LIKELY_STARTER,
            reason=_STABLE_REASON,
        )
        for fpl_id in range(1, 16)
    }


def test_no_rotation_risk_players_yields_a_stable_label():
    squad = _validate(_players())
    projections = tuple(_projections())

    sensitivity = evaluate_role_scenario_sensitivity(
        squad, projections, role_state_by_id=_role_states(set())
    )

    assert sensitivity.scenarios_considered == ()
    assert sensitivity.is_sensitive is False
    assert sensitivity.label == "stable"
    assert sensitivity.report["scenarios_considered"] == 0
    assert sensitivity.report["scenarios_that_change_the_recommendation"] == []


def test_marginal_starter_blanking_flips_the_starting_xi_and_is_sensitive():
    squad = _validate(_players())
    projections = tuple(_projections())
    # Player 11 (fpl_id 11, MID, 6.5 xpts) is the weakest starter in the base
    # recommendation; player 15 (MID, 3.5 xpts) is the strongest outfield
    # bench player and a same-position, formation-legal replacement.
    sensitivity = evaluate_role_scenario_sensitivity(
        squad, projections, role_state_by_id=_role_states({11})
    )

    assert len(sensitivity.scenarios_considered) == 1
    scenario = sensitivity.scenarios_considered[0]
    assert scenario.fpl_id == 11
    assert scenario.starting_xi_changed is True
    assert sensitivity.is_sensitive is True
    assert sensitivity.label == "sensitive"
    changed = sensitivity.report["scenarios_that_change_the_recommendation"]
    assert [row["fpl_id"] for row in changed] == [11]


def test_bench_rotation_risk_player_blanking_does_not_change_anything():
    squad = _validate(_players())
    projections = tuple(_projections())
    # Player 13 (fpl_id 13, DEF, 3.0 xpts) is already on the bench in the
    # base recommendation and is not the strongest outfield bench option, so
    # blanking their projection changes nothing about who starts or captains.
    sensitivity = evaluate_role_scenario_sensitivity(
        squad, projections, role_state_by_id=_role_states({13})
    )

    assert len(sensitivity.scenarios_considered) == 1
    scenario = sensitivity.scenarios_considered[0]
    assert scenario.fpl_id == 13
    assert scenario.recommendation_changed is False
    assert sensitivity.is_sensitive is False
    assert sensitivity.label == "stable"


def test_captain_blanking_changes_the_captain_but_not_necessarily_the_xi():
    squad = _validate(_players())
    projections = tuple(_projections())
    # Player 9 (fpl_id 9, FWD, 10.0 xpts) is the base captain. Blanking them
    # keeps them in the legal XI (still needed for the >=1 FWD constraint)
    # but a different starter becomes the highest scorer and thus captain.
    sensitivity = evaluate_role_scenario_sensitivity(
        squad, projections, role_state_by_id=_role_states({9})
    )

    scenario = sensitivity.scenarios_considered[0]
    assert scenario.fpl_id == 9
    assert scenario.captain_changed is True
    assert sensitivity.is_sensitive is True


def test_multiple_rotation_risk_players_are_each_perturbed_independently():
    squad = _validate(_players())
    projections = tuple(_projections())

    sensitivity = evaluate_role_scenario_sensitivity(
        squad, projections, role_state_by_id=_role_states({11, 13})
    )

    assert {scenario.fpl_id for scenario in sensitivity.scenarios_considered} == {11, 13}
    assert sensitivity.is_sensitive is True


def test_role_state_missing_for_a_squad_member_is_treated_as_not_rotation_risk():
    squad = _validate(_players())
    projections = tuple(_projections())
    role_states = _role_states({11})
    del role_states[11]

    sensitivity = evaluate_role_scenario_sensitivity(
        squad, projections, role_state_by_id=role_states
    )

    assert sensitivity.scenarios_considered == ()
    assert sensitivity.label == "stable"


def test_base_recommendation_can_be_reused_instead_of_recomputed():
    from fpl_model.decision.lineup import recommend_lineup

    squad = _validate(_players())
    projections = tuple(_projections())
    base = recommend_lineup(squad, projections)

    sensitivity = evaluate_role_scenario_sensitivity(
        squad,
        projections,
        role_state_by_id=_role_states({11}),
        base_recommendation=base,
    )

    assert sensitivity.base_recommendation is base
