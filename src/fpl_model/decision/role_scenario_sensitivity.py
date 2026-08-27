"""Flag a lineup recommendation as `sensitive` when it depends on a rotation guess.

P0 (`README.md`'s "Production critical path") requires comparing the base
recommendation against plausible role scenarios and, when the recommendation
changes, labelling the decision `sensitive` rather than presenting an
unconditional "Best option". `validation/role_state.py` already identifies
which squad members carry genuine rotation risk (`ROTATION`); this module
asks a narrow, concrete question about each of them: if this specific player
blanked (0 minutes, exactly as FPL's own autosub rule keys off -- see
`decision/autosub.py`), would the recommended starting XI or captain actually
change?

A player who is comfortably a starter or comfortably a bench player never
produces a scenario here -- perturbing their outcome would not plausibly
change anything, and enumerating it would just add noise a manager has to
read past. Only `ROTATION`-state players are perturbed, one at a time, so a
manager can see exactly WHICH player's uncertainty is driving the warning.

This module never changes what `recommend_lineup` returns for the base
scenario; it only labels the result and names the plausible alternative that
would flip it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fpl_model.decision.lineup import (
    LineupRecommendation,
    PlayerGameweekProjection,
    recommend_lineup,
)
from fpl_model.decision.squad import ValidatedSquad
from fpl_model.validation.role_state import ROTATION, RoleStateResult

# A rotation-risk player's "blanks entirely" counterfactual: FPL's own
# autosub rule keys off exactly 0 minutes for the whole Gameweek, so this is
# the single most decision-relevant alternative to check, rather than an
# arbitrary partial markdown of their projection.
BLANK_EXPECTED_POINTS = 0.0
BLANK_APPEARANCE_PROBABILITY = 0.0


@dataclass(frozen=True, slots=True)
class RoleScenarioOutcome:
    fpl_id: int
    player_name: str
    starting_xi_changed: bool
    captain_changed: bool

    @property
    def recommendation_changed(self) -> bool:
        return self.starting_xi_changed or self.captain_changed


@dataclass(frozen=True, slots=True)
class RoleScenarioSensitivity:
    base_recommendation: LineupRecommendation
    scenarios_considered: tuple[RoleScenarioOutcome, ...]

    @property
    def is_sensitive(self) -> bool:
        return any(scenario.recommendation_changed for scenario in self.scenarios_considered)

    @property
    def label(self) -> str:
        return "sensitive" if self.is_sensitive else "stable"

    @property
    def report(self) -> dict[str, Any]:
        changed = [
            scenario for scenario in self.scenarios_considered if scenario.recommendation_changed
        ]
        return {
            "label": self.label,
            "scenarios_considered": len(self.scenarios_considered),
            "scenarios_that_change_the_recommendation": [
                {
                    "fpl_id": scenario.fpl_id,
                    "player_name": scenario.player_name,
                    "starting_xi_changed": scenario.starting_xi_changed,
                    "captain_changed": scenario.captain_changed,
                }
                for scenario in changed
            ],
            "note": (
                "Compares the base recommendation against a 'this rotation-risk "
                "player blanks' counterfactual for each ROTATION-state squad "
                "member, one at a time. 'sensitive' means at least one such "
                "plausible scenario changes the starting XI or captain -- treat "
                "the base recommendation as conditional on that player's "
                "involvement, not as an unconditional best option."
                if self.is_sensitive
                else "Compares the base recommendation against a 'this "
                "rotation-risk player blanks' counterfactual for each "
                "ROTATION-state squad member, one at a time. None changed the "
                "starting XI or captain."
            ),
        }


def _blanked_projection(projection: PlayerGameweekProjection) -> PlayerGameweekProjection:
    return PlayerGameweekProjection(
        fpl_id=projection.fpl_id,
        expected_points=BLANK_EXPECTED_POINTS,
        uncertainty=projection.uncertainty,
        data_quality_flags=projection.data_quality_flags,
        appearance_probability=BLANK_APPEARANCE_PROBABILITY,
    )


def evaluate_role_scenario_sensitivity(
    squad: ValidatedSquad,
    projections: tuple[PlayerGameweekProjection, ...],
    *,
    role_state_by_id: dict[int, RoleStateResult],
    base_recommendation: LineupRecommendation | None = None,
) -> RoleScenarioSensitivity:
    """Compare the base recommendation against a blank-scenario for each rotation-risk player.

    ``role_state_by_id`` is the same mapping `recommend_lineup.py` already
    loads from `validation.role_state.load_role_states` for display -- this
    function does not recompute role state itself, so the two never drift.
    Only players in the squad who are also present in ``role_state_by_id``
    with state `ROTATION` are perturbed.
    """
    if base_recommendation is None:
        base_recommendation = recommend_lineup(squad, projections)

    player_name_by_id = {player.fpl_id: player.player_name for player in squad.players}
    base_starter_ids = {player.fpl_id for player in base_recommendation.starters}
    base_captain_id = base_recommendation.captain.fpl_id

    rotation_ids = sorted(
        fpl_id
        for fpl_id in player_name_by_id
        if role_state_by_id.get(fpl_id) is not None
        and role_state_by_id[fpl_id].role_state == ROTATION
    )

    scenarios: list[RoleScenarioOutcome] = []
    for fpl_id in rotation_ids:
        scenario_projections = tuple(
            _blanked_projection(projection) if projection.fpl_id == fpl_id else projection
            for projection in projections
        )
        scenario_recommendation = recommend_lineup(squad, scenario_projections)
        scenario_starter_ids = {player.fpl_id for player in scenario_recommendation.starters}
        scenarios.append(
            RoleScenarioOutcome(
                fpl_id=fpl_id,
                player_name=player_name_by_id[fpl_id],
                starting_xi_changed=scenario_starter_ids != base_starter_ids,
                captain_changed=scenario_recommendation.captain.fpl_id != base_captain_id,
            )
        )

    return RoleScenarioSensitivity(
        base_recommendation=base_recommendation,
        scenarios_considered=tuple(scenarios),
    )
