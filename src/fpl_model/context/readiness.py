"""Context features that describe readiness without directly modifying xPts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class TournamentReadiness:
    tournament_minutes: float = 0.0
    last_tournament_match: date | None = None
    club_return_date: date | None = None
    preseason_minutes: float = 0.0

    def features(self, as_of: date) -> dict[str, float | int | None]:
        """Create deadline-safe descriptive features for later calibration."""
        days_since_tournament = (
            (as_of - self.last_tournament_match).days
            if self.last_tournament_match is not None
            else None
        )
        training_days = (
            max(0, (as_of - self.club_return_date).days)
            if self.club_return_date is not None
            else None
        )
        return {
            "tournament_minutes": float(self.tournament_minutes),
            "days_since_last_tournament_match": days_since_tournament,
            "training_days": training_days,
            "preseason_minutes": float(self.preseason_minutes),
        }
