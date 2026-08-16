from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from fpl_model.ingest.sofascore_experimental import (
    SofaScoreCoverageError,
    SofaScoreExperimentalClient,
)


@dataclass
class DummyResponse:
    payload: dict[str, Any]
    status_code: int = 200

    def json(self) -> dict[str, Any]:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class DummySession:
    def __init__(self, responses: dict[str, DummyResponse]):
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str, **_: Any) -> DummyResponse:
        self.calls.append(url)
        for suffix, response in self.responses.items():
            if url.endswith(suffix):
                return response
        raise AssertionError(f"Unexpected URL: {url}")


def client_with(responses: dict[str, DummyResponse]) -> SofaScoreExperimentalClient:
    return SofaScoreExperimentalClient(
        base_url="https://example.test/api/v1",
        min_interval_seconds=0.0,
        session=DummySession(responses),  # type: ignore[arg-type]
    )


def test_discovers_team_event_from_daily_schedule():
    client = client_with(
        {
            "sport/football/scheduled-events/2026-08-15": DummyResponse(
                {
                    "events": [
                        {
                            "id": 101,
                            "homeTeam": {"id": 38, "name": "Chelsea"},
                            "awayTeam": {"id": 2824, "name": "Real Sociedad"},
                        },
                        {
                            "id": 102,
                            "homeTeam": {"id": 1},
                            "awayTeam": {"id": 2},
                        },
                    ]
                }
            )
        }
    )

    events = client.find_team_events("2026-08-15", team_id=38)
    assert [event["id"] for event in events] == [101]


def test_flattens_lineups_and_normalises_heatmap():
    client = client_with(
        {
            "event/101/lineups": DummyResponse(
                {
                    "home": {
                        "players": [
                            {
                                "player": {
                                    "id": 9001,
                                    "name": "Example Player",
                                    "shortName": "E. Player",
                                    "position": "D",
                                },
                                "substitute": False,
                            }
                        ]
                    },
                    "away": {"players": []},
                }
            ),
            "event/101/player/9001/heatmap": DummyResponse(
                {"heatmap": [{"x": 75, "y": 90}, {"x": 90, "y": 50}]}
            ),
        }
    )

    players = client.lineup_players(101)
    assert players.loc[0, "sofascore_player_id"] == 9001
    assert bool(players.loc[0, "starter"]) is True

    points = client.normalised_player_heatmap(101, 9001)
    assert points.tolist() == [[0.75, 0.9], [0.9, 0.5]]


def test_missing_heatmap_fails_gracefully():
    client = client_with(
        {
            "event/101/player/9001/heatmap": DummyResponse({}, status_code=404),
        }
    )

    with pytest.raises(SofaScoreCoverageError):
        client.player_heatmap(101, 9001)
