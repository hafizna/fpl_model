"""Adapter for the Vaastav Fantasy-Premier-League upstream repository."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO

import pandas as pd
import requests

RAW_BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"


@dataclass(slots=True)
class VaastavClient:
    raw_base: str = RAW_BASE
    timeout_seconds: float = 60.0

    def _csv(self, relative_path: str) -> pd.DataFrame:
        url = f"{self.raw_base.rstrip('/')}/{relative_path.lstrip('/')}"
        response = requests.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        return pd.read_csv(StringIO(response.text))

    def cleaned_players(self, season: str) -> pd.DataFrame:
        return self._csv(f"{season}/cleaned_players.csv")

    def players_raw(self, season: str) -> pd.DataFrame:
        return self._csv(f"{season}/players_raw.csv")

    def fixtures(self, season: str) -> pd.DataFrame:
        return self._csv(f"{season}/fixtures.csv")

    def teams(self, season: str) -> pd.DataFrame:
        return self._csv(f"{season}/teams.csv")

    def player_id_list(self, season: str) -> pd.DataFrame:
        return self._csv(f"{season}/player_idlist.csv")

    def merged_gameweeks(self, season: str) -> pd.DataFrame:
        """Load the season's merged gameweek file when upstream provides it."""
        candidates = [
            f"{season}/gws/merged_gw.csv",
            f"{season}/gws/merged_gws.csv",
        ]
        errors: list[str] = []
        for path in candidates:
            try:
                return self._csv(path)
            except requests.HTTPError as exc:
                errors.append(f"{path}: {exc}")
        raise FileNotFoundError(
            "Could not find a merged gameweek file. Tried: " + "; ".join(errors)
        )

    def world_cup_2026(self) -> pd.DataFrame:
        return self._csv("world_cup_2026.csv")


def add_season_column(frame: pd.DataFrame, season: str) -> pd.DataFrame:
    """Return a copy tagged with season without mutating the upstream frame."""
    result = frame.copy()
    result.insert(0, "season", season)
    return result
