from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
import requests

from fpl_model.ingest.sofascore_experimental import (
    DEFAULT_BASE_URL,
    SofaScoreCoverageError,
    SofaScoreExperimentalClient,
    SofaScoreTransportError,
)


@dataclass
class DummyResponse:
    payload: dict[str, Any]
    status_code: int = 200

    def json(self) -> dict[str, Any]:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")


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


class SSLFailingSession:
    def get(self, url: str, **_: Any) -> DummyResponse:
        raise requests.exceptions.SSLError(f"certificate mismatch for {url}")


def client_with(responses: dict[str, DummyResponse]) -> SofaScoreExperimentalClient:
    return SofaScoreExperimentalClient(
        base_url="https://example.test/api/v1",
        min_interval_seconds=0.0,
        session=DummySession(responses),  # type: ignore[arg-type]
    )


def test_default_uses_dedicated_api_hostname():
    assert DEFAULT_BASE_URL == "https://api.sofascore.com/api/v1"


def test_tls_failure_is_reported_as_transport_error():
    client = SofaScoreExperimentalClient(
        min_interval_seconds=0.0,
        session=SSLFailingSession(),  # type: ignore[arg-type]
    )

    with pytest.raises(SofaScoreTransportError, match="TLS verification failed"):
        client.scheduled_events("2026-08-15")


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


def test_flattens_match_level_average_positions():
    client = client_with(
        {
            "event/101/average-positions": DummyResponse(
                {
                    "home": [
                        {
                            "player": {
                                "id": 9001,
                                "name": "Example Wing Back",
                                "shortName": "E. Wing Back",
                                "position": "D",
                            },
                            "averageX": 77,
                            "averageY": 88,
                        }
                    ],
                    "away": [],
                }
            )
        }
    )

    positions = client.normalised_average_positions(101)
    assert positions.loc[0, "sofascore_player_id"] == 9001
    assert positions.loc[0, "normalised_x"] == pytest.approx(0.77)
    assert positions.loc[0, "normalised_y"] == pytest.approx(0.88)


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
