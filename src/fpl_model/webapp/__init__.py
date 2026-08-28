"""Application-facing services for the browser recommender."""

from fpl_model.webapp.service import (
    CurrentSquadSetup,
    ResearchHorizon,
    RoleScenarioOverride,
    apply_role_scenario_overrides,
    load_release_catalog,
    load_web_bootstrap,
    recommend_web_lineups,
    recommend_web_transfers,
    resolve_entry_picks,
)

__all__ = [
    "CurrentSquadSetup",
    "ResearchHorizon",
    "RoleScenarioOverride",
    "apply_role_scenario_overrides",
    "load_release_catalog",
    "load_web_bootstrap",
    "recommend_web_lineups",
    "recommend_web_transfers",
    "resolve_entry_picks",
]
