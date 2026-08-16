"""Canonical records shared across upstream providers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class PlayerIdentity:
    fpl_id: int
    player_code: int | None
    name: str
    team_id: int
    fpl_position: str
    season: str


@dataclass(frozen=True, slots=True)
class MatchIdentity:
    match_id: str
    match_date: date
    competition: str
    season: str
    team_id: int | str
    opponent_id: int | str
    home_away: str
    gameweek: int | None = None


@dataclass(frozen=True, slots=True)
class PreseasonAppearance:
    player_id: int
    match_id: str
    started: bool
    minutes: float
    sub_on_minute: float | None = None
    sub_off_minute: float | None = None
    nominal_position: str | None = None
    nominal_formation: str | None = None
    manager: str | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.minutes <= 130:
            raise ValueError("minutes must be between 0 and 130")
        if self.sub_on_minute is not None and self.sub_on_minute < 0:
            raise ValueError("sub_on_minute cannot be negative")
        if self.sub_off_minute is not None and self.sub_off_minute < 0:
            raise ValueError("sub_off_minute cannot be negative")
