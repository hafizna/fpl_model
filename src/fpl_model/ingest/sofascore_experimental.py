"""Experimental SofaScore adapter for preseason spatial research.

SofaScore's web endpoints are undocumented and may change without notice. Keep
this adapter isolated from canonical model code and fail gracefully when match
coverage is absent. The model should never require this provider to run.

Community projects currently document endpoints such as:

- /sport/football/scheduled-events/{YYYY-MM-DD}
- /event/{event_id}/lineups
- /event/{event_id}/average-positions
- /event/{event_id}/player/{player_id}/heatmap
- /event/{event_id}/player/{player_id}/rating-breakdown

Average positions are attractive as the first spatial signal because one match-
level request can return all players. Player heatmaps are denser but require a
request per player. Community references describe these pitch coordinates on a
0..100 grid. Coordinate orientation must still be verified against known matches
before derived role metrics are treated as calibrated model features.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from time import monotonic, sleep
from typing import Any

import numpy as np
import pandas as pd
import requests

from fpl_model.tactics.spatial import normalise_points

DEFAULT_BASE_URL = "https://www.sofascore.com/api/v1"


class SofaScoreCoverageError(RuntimeError):
    """Raised when an event/player lacks the requested provider coverage."""


@dataclass(slots=True)
class SofaScoreExperimentalClient:
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 30.0
    min_interval_seconds: float = 0.75
    user_agent: str = "fpl-model/0.1 research client"
    session: requests.Session = field(default_factory=requests.Session, repr=False)
    _last_request_at: float = field(default=0.0, init=False, repr=False)

    def _throttle(self) -> None:
        elapsed = monotonic() - self._last_request_at
        wait = self.min_interval_seconds - elapsed
        if wait > 0:
            sleep(wait)

    def _get_json(self, path: str) -> dict[str, Any]:
        self._throttle()
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        response = self.session.get(
            url,
            timeout=self.timeout_seconds,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
        )
        self._last_request_at = monotonic()

        if response.status_code == 404:
            raise SofaScoreCoverageError(f"No SofaScore coverage for {path}")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Expected JSON object from {path}")
        return payload

    def scheduled_events(self, when: date | str) -> list[dict[str, Any]]:
        date_text = when.isoformat() if isinstance(when, date) else when
        payload = self._get_json(f"sport/football/scheduled-events/{date_text}")
        events = payload.get("events", [])
        if not isinstance(events, list):
            raise ValueError("SofaScore scheduled-events payload changed shape")
        return events

    def find_team_events(self, when: date | str, team_id: int) -> list[dict[str, Any]]:
        """Discover event IDs for one team from the daily schedule."""
        matches: list[dict[str, Any]] = []
        for event in self.scheduled_events(when):
            home_id = event.get("homeTeam", {}).get("id")
            away_id = event.get("awayTeam", {}).get("id")
            if team_id in {home_id, away_id}:
                matches.append(event)
        return matches

    def event(self, event_id: int) -> dict[str, Any]:
        return self._get_json(f"event/{event_id}")

    def lineups(self, event_id: int) -> dict[str, Any]:
        return self._get_json(f"event/{event_id}/lineups")

    def lineup_players(self, event_id: int) -> pd.DataFrame:
        """Flatten available home/away lineup player identities."""
        payload = self.lineups(event_id)
        rows: list[dict[str, Any]] = []

        for side in ("home", "away"):
            side_payload = payload.get(side, {})
            players = side_payload.get("players", []) if isinstance(side_payload, dict) else []
            for item in players:
                player = item.get("player", {})
                if not player:
                    continue
                rows.append(
                    {
                        "event_id": event_id,
                        "side": side,
                        "sofascore_player_id": player.get("id"),
                        "player_name": player.get("name"),
                        "short_name": player.get("shortName"),
                        "provider_position": player.get("position"),
                        "starter": item.get("substitute") is not True,
                    }
                )

        return pd.DataFrame(rows)

    def average_positions(self, event_id: int) -> dict[str, Any]:
        """Return the raw match-level average-position payload."""
        return self._get_json(f"event/{event_id}/average-positions")

    def average_positions_table(self, event_id: int) -> pd.DataFrame:
        """Flatten averageX/averageY for all covered players in one request."""
        payload = self.average_positions(event_id)
        rows: list[dict[str, Any]] = []

        for side in ("home", "away"):
            items = payload.get(side, [])
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                player = item.get("player", {})
                if not isinstance(player, dict) or not player:
                    continue
                rows.append(
                    {
                        "event_id": event_id,
                        "side": side,
                        "sofascore_player_id": player.get("id"),
                        "player_name": player.get("name"),
                        "short_name": player.get("shortName"),
                        "provider_position": player.get("position"),
                        "average_x": item.get("averageX"),
                        "average_y": item.get("averageY"),
                    }
                )

        frame = pd.DataFrame(rows)
        if frame.empty:
            raise SofaScoreCoverageError(f"Average positions unavailable for event={event_id}")

        for column in ("average_x", "average_y"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame

    def normalised_average_positions(
        self,
        event_id: int,
        *,
        flip_x: bool = False,
        flip_y: bool = False,
    ) -> pd.DataFrame:
        """Return average positions on the project's 0..1 pitch convention."""
        frame = self.average_positions_table(event_id).copy()
        valid = frame[["average_x", "average_y"]].notna().all(axis=1)
        frame["normalised_x"] = np.nan
        frame["normalised_y"] = np.nan

        if valid.any():
            points = list(
                zip(
                    frame.loc[valid, "average_x"],
                    frame.loc[valid, "average_y"],
                    strict=True,
                )
            )
            normalised = normalise_points(
                points,
                x_max=100.0,
                y_max=100.0,
                flip_x=flip_x,
                flip_y=flip_y,
            )
            frame.loc[valid, "normalised_x"] = normalised[:, 0]
            frame.loc[valid, "normalised_y"] = normalised[:, 1]

        return frame

    def player_heatmap(self, event_id: int, player_id: int) -> list[tuple[float, float]]:
        payload = self._get_json(f"event/{event_id}/player/{player_id}/heatmap")
        raw = payload.get("heatmap")
        if not isinstance(raw, list) or not raw:
            raise SofaScoreCoverageError(
                f"Heatmap unavailable for event={event_id}, player={player_id}"
            )

        points: list[tuple[float, float]] = []
        for item in raw:
            if not isinstance(item, dict) or "x" not in item or "y" not in item:
                continue
            points.append((float(item["x"]), float(item["y"])))

        if not points:
            raise SofaScoreCoverageError(
                f"Heatmap contained no valid coordinates for event={event_id}, player={player_id}"
            )
        return points

    def normalised_player_heatmap(
        self,
        event_id: int,
        player_id: int,
        *,
        flip_x: bool = False,
        flip_y: bool = False,
    ) -> np.ndarray:
        """Normalise raw provider heatmap points from 0..100 to 0..1."""
        points = self.player_heatmap(event_id, player_id)
        return normalise_points(
            points,
            x_max=100.0,
            y_max=100.0,
            flip_x=flip_x,
            flip_y=flip_y,
        )

    def player_rating_breakdown(self, event_id: int, player_id: int) -> dict[str, Any]:
        return self._get_json(f"event/{event_id}/player/{player_id}/rating-breakdown")
