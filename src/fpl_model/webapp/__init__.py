"""Application-facing services for the browser recommender."""

from fpl_model.webapp.service import (
    ResearchHorizon,
    load_web_bootstrap,
    recommend_web_lineups,
    recommend_web_transfers,
)

__all__ = [
    "ResearchHorizon",
    "load_web_bootstrap",
    "recommend_web_lineups",
    "recommend_web_transfers",
]
