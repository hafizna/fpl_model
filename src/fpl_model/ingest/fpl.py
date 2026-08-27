"""Official Fantasy Premier League API adapter.

This module intentionally exposes raw tables and a small canonical player table.
It does not calculate projections. Keeping ingestion separate from modelling makes
backtests easier to audit and reduces accidental feature leakage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import requests

DEFAULT_BASE_URL = "https://fantasy.premierleague.com/api/"
POSITION_MAP = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD", 5: "AM"}


@dataclass(slots=True)
class FPLClient:
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 30.0

    def _get_json(self, path: str) -> Any:
        url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        response = requests.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        return response.json()

    def bootstrap(self) -> dict[str, Any]:
        return self._get_json("bootstrap-static/")

    def fixtures(self) -> pd.DataFrame:
        return pd.DataFrame(self._get_json("fixtures/"))

    def snapshot_payload(self) -> tuple[dict[str, Any], list[dict[str, Any]], datetime]:
        """Fetch one current player/team/event payload and fixture payload."""
        bootstrap = self.bootstrap()
        fixtures = self._get_json("fixtures/")
        if not isinstance(bootstrap, dict) or not isinstance(fixtures, list):
            raise ValueError("FPL API snapshot payload changed shape")
        return bootstrap, fixtures, datetime.now(UTC)

    def event_live(self, gameweek: int) -> dict[str, Any]:
        """Fetch official per-player statistics for one gameweek."""
        if not 1 <= gameweek <= 38:
            raise ValueError("gameweek must be between 1 and 38")
        payload = self._get_json(f"event/{gameweek}/live/")
        if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
            raise ValueError("FPL event-live payload changed shape")
        return payload

    def entry(self, entry_id: int) -> dict[str, Any]:
        """Fetch one manager's public profile (`name`, `current_event`, etc.).

        Public, no authentication required -- distinct from `my-team/{id}/`,
        which is the private, editable view and requires a login session.
        """
        if entry_id <= 0:
            raise ValueError("entry_id must be positive")
        payload = self._get_json(f"entry/{entry_id}/")
        if not isinstance(payload, dict) or "id" not in payload:
            raise ValueError("FPL entry payload changed shape")
        return payload

    def entry_picks(self, entry_id: int, gameweek: int) -> dict[str, Any]:
        """Fetch one manager's public squad picks for one Gameweek.

        Public, no authentication required. The `picks` list carries only
        `element`/`position`/`multiplier`/`is_captain`/`is_vice_captain`/
        `element_type` -- no per-player purchase or selling price. `bank` and
        `value` (both FPL's own tenths-of-a-million unit) live under
        `entry_history`.
        """
        if entry_id <= 0:
            raise ValueError("entry_id must be positive")
        if not 1 <= gameweek <= 38:
            raise ValueError("gameweek must be between 1 and 38")
        payload = self._get_json(f"entry/{entry_id}/event/{gameweek}/picks/")
        if not isinstance(payload, dict) or not isinstance(payload.get("picks"), list):
            raise ValueError("FPL entry picks payload changed shape")
        return payload

    def raw_players(self) -> pd.DataFrame:
        return pd.DataFrame(self.bootstrap()["elements"])

    def teams(self) -> pd.DataFrame:
        return pd.DataFrame(self.bootstrap()["teams"])

    def canonical_players(self) -> pd.DataFrame:
        """Return stable, modelling-oriented player identity fields.

        This table is deliberately small. Match/GW performance is stored in
        separate fact tables rather than continuously appended to the identity
        record.
        """
        payload = self.bootstrap()
        players = pd.DataFrame(payload["elements"]).copy()
        teams = pd.DataFrame(payload["teams"])[["id", "name", "short_name"]].copy()

        keep = [
            "id",
            "code",
            "first_name",
            "second_name",
            "web_name",
            "team",
            "element_type",
            "now_cost",
            "status",
        ]
        players = players[keep]
        players["fpl_position"] = players["element_type"].map(POSITION_MAP)
        players["price"] = pd.to_numeric(players["now_cost"], errors="coerce") / 10.0

        players = players.merge(
            teams,
            left_on="team",
            right_on="id",
            how="left",
            suffixes=("", "_team"),
            validate="many_to_one",
        )

        return players.rename(
            columns={
                "id": "fpl_id",
                "code": "player_code",
                "team": "team_id",
                "name": "team_name",
                "short_name": "team_short",
            }
        )[
            [
                "fpl_id",
                "player_code",
                "first_name",
                "second_name",
                "web_name",
                "team_id",
                "team_name",
                "team_short",
                "fpl_position",
                "price",
                "status",
            ]
        ]


def main() -> None:
    client = FPLClient()
    players = client.canonical_players()
    fixtures = client.fixtures()
    print(f"Loaded {len(players)} FPL players and {len(fixtures)} fixtures")
    print(players.head().to_string(index=False))


if __name__ == "__main__":
    main()
