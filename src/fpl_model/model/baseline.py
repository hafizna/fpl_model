"""Composition of component projections into one explainable baseline total."""

from __future__ import annotations

from dataclasses import dataclass

from fpl_model.model.appearance import AppearanceProjection
from fpl_model.model.attacking import AttackingProjection
from fpl_model.model.defence import DefensiveProjection
from fpl_model.model.fixture import (
    FixtureContext,
    ScoringComponents,
    apply_home_away_once,
)
from fpl_model.model.secondary import WeightedExpectedPoints


@dataclass(frozen=True, slots=True)
class BaselineComponentProjections:
    """Unconditional outputs from each independently tested component model."""

    appearance: AppearanceProjection
    attacking: AttackingProjection
    defensive: DefensiveProjection
    saves: WeightedExpectedPoints
    yellow_cards: WeightedExpectedPoints
    red_cards: WeightedExpectedPoints
    bonus: WeightedExpectedPoints
    defcon: WeightedExpectedPoints

    def scoring_components(self) -> ScoringComponents:
        """Flatten typed projections without changing any component value."""
        return ScoringComponents(
            appearance=self.appearance.appearance_xpts,
            sixty_minutes=self.appearance.sixty_minute_xpts,
            saves=self.saves.total_xpts,
            yellow_cards=self.yellow_cards.total_xpts,
            red_cards=self.red_cards.total_xpts,
            bonus=self.bonus.total_xpts,
            assists=self.attacking.assist_xpts,
            goals=self.attacking.goal_xpts,
            clean_sheet=self.defensive.clean_sheet_xpts,
            goals_conceded=self.defensive.goals_conceded_xpts,
            defcon=self.defcon.total_xpts,
        )


@dataclass(frozen=True, slots=True)
class BaselineProjection:
    """Auditable end-to-end xPts for one player-fixture."""

    fixture: FixtureContext
    source_projections: BaselineComponentProjections
    components: ScoringComponents
    pre_home_away_xpts: float
    home_away_multiplier: float
    total_xpts: float


def compose_baseline_projection(
    fixture: FixtureContext,
    projections: BaselineComponentProjections,
) -> BaselineProjection:
    """Compose unconditional components and apply home/away exactly once."""
    components = projections.scoring_components()
    adjusted = apply_home_away_once(
        components.total_xpts,
        is_home=fixture.is_home,
    )
    return BaselineProjection(
        fixture=fixture,
        source_projections=projections,
        components=components,
        pre_home_away_xpts=adjusted.pre_home_away_xpts,
        home_away_multiplier=adjusted.home_away_multiplier,
        total_xpts=adjusted.total_xpts,
    )
