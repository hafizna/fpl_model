"""Coverage for FPLClient's public entry/picks fetching methods.

No HTTP-mocking library is installed in this project; `_get_json` is
monkeypatched directly, mirroring how the rest of the ingest layer keeps
persistence tests entirely payload-driven without touching the network.
"""

from __future__ import annotations

import pytest

from fpl_model.ingest.fpl import FPLClient


def test_entry_fetches_the_manager_profile_path(monkeypatch):
    client = FPLClient()
    captured_paths: list[str] = []

    def fake_get_json(self, path):
        captured_paths.append(path)
        return {"id": 123456, "name": "Test Team", "current_event": 2}

    monkeypatch.setattr(FPLClient, "_get_json", fake_get_json)

    payload = client.entry(123456)

    assert captured_paths == ["entry/123456/"]
    assert payload["name"] == "Test Team"


def test_entry_rejects_non_positive_entry_id():
    client = FPLClient()
    with pytest.raises(ValueError, match="entry_id must be positive"):
        client.entry(0)


def test_entry_rejects_a_payload_missing_id(monkeypatch):
    client = FPLClient()
    monkeypatch.setattr(FPLClient, "_get_json", lambda self, path: {"name": "no id field"})

    with pytest.raises(ValueError, match="changed shape"):
        client.entry(1)


def test_entry_picks_fetches_the_correct_path(monkeypatch):
    client = FPLClient()
    captured_paths: list[str] = []

    def fake_get_json(self, path):
        captured_paths.append(path)
        return {
            "active_chip": None,
            "entry_history": {"bank": 5, "value": 1005, "event": 2},
            "picks": [
                {
                    "element": 101,
                    "position": 1,
                    "multiplier": 1,
                    "is_captain": False,
                    "is_vice_captain": False,
                    "element_type": 1,
                }
            ],
        }

    monkeypatch.setattr(FPLClient, "_get_json", fake_get_json)

    payload = client.entry_picks(123456, 2)

    assert captured_paths == ["entry/123456/event/2/picks/"]
    assert len(payload["picks"]) == 1
    assert payload["entry_history"]["bank"] == 5


def test_entry_picks_rejects_out_of_range_gameweek():
    client = FPLClient()
    with pytest.raises(ValueError, match="gameweek must be between 1 and 38"):
        client.entry_picks(1, 0)


def test_entry_picks_rejects_a_payload_without_a_picks_list(monkeypatch):
    client = FPLClient()
    monkeypatch.setattr(FPLClient, "_get_json", lambda self, path: {"entry_history": {}})

    with pytest.raises(ValueError, match="changed shape"):
        client.entry_picks(1, 1)
