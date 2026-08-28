"""Coverage for webapp.service._lineup_payload's role_scenario_sensitivity wiring.

Builds its own compact-release fixture (rather than reusing
test_webapp_service.py's own `_release_file`, which has no `role_state`
field) with one ROTATION-state player whose blanking is expected to flip
the recommended starting XI, so the wiring can be verified against a real,
predictable "sensitive" outcome -- not just "the field exists".
"""

from __future__ import annotations

import json
from pathlib import Path

from fpl_model.validation.role_state import LIKELY_STARTER, ROTATION
from fpl_model.webapp.service import recommend_web_lineups, recommend_web_transfers


def _release_with_role_state(path: Path) -> tuple[int, ...]:
    positions = ("GK", "GK", *("DEF",) * 5, *("MID",) * 5, *("FWD",) * 3)
    players = []
    for fpl_id, position in enumerate(positions, start=1):
        # fpl_id 11 (the weakest starter, lowest xpts among starters 1-11)
        # is marked ROTATION; everyone else is LIKELY_STARTER. Player 15
        # (MID, the strongest bench option) is the same-position replacement
        # that should take fpl_id 11's place once fpl_id 11 blanks.
        role_state = ROTATION if fpl_id == 11 else LIKELY_STARTER
        players.append(
            {
                "fpl_id": fpl_id,
                "player_code": 10_000 + fpl_id,
                "name": f"Player {fpl_id}",
                "team_id": ((fpl_id - 1) % 6) + 1,
                "team": f"T{((fpl_id - 1) % 6) + 1}",
                "position": position,
                "price_tenths": 50,
                "status": "a",
                "gameweeks": {
                    str(gameweek): {
                        "xpts": float(fpl_id),
                        "appearance_probability": 0.95,
                        "uncertainty": None,
                        "quality_flags": [],
                        "role_state": {
                            "role_state": role_state,
                            "reason": "test fixture",
                        },
                    }
                    for gameweek in (2, 3, 4)
                },
            }
        )
    payload = {
        "schema_version": "fpl_web_release_v1",
        "release": {
            "health": "shadow",
            "source_ingestion_run_id": path.stem,
            "model_version": "web_test_v1",
            "planning_as_of": "2026-08-26T08:00:00+00:00",
            "model_runs": [
                {"gameweek": gameweek, "model_run_id": f"run_gw{gameweek}"}
                for gameweek in (2, 3, 4)
            ],
        },
        "players": players,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return tuple(range(1, 16))


def test_recommend_web_lineups_flags_a_sensitive_baseline_recommendation(tmp_path: Path):
    release_path = tmp_path / "release.json"
    fpl_ids = _release_with_role_state(release_path)

    result = recommend_web_lineups(fpl_ids, release_path=release_path)

    first_gw = result["lineups"][0]
    sensitivity = first_gw["role_scenario_sensitivity"]
    assert sensitivity is not None
    assert sensitivity["label"] == "sensitive"
    flagged_ids = {
        row["fpl_id"] for row in sensitivity["scenarios_that_change_the_recommendation"]
    }
    assert 11 in flagged_ids


def test_recommend_web_transfers_flags_baseline_sensitivity_but_not_candidates(
    tmp_path: Path,
):
    release_path = tmp_path / "release.json"
    fpl_ids = _release_with_role_state(release_path)

    result = recommend_web_transfers(fpl_ids, release_path=release_path)

    baseline_sensitivity = result["baseline_lineups"][0]["role_scenario_sensitivity"]
    assert baseline_sensitivity is not None
    assert baseline_sensitivity["label"] == "sensitive"

    # Candidate transfer lineups deliberately skip the (expensive)
    # sensitivity re-computation -- see the comment in
    # webapp.service.recommend_web_transfers.
    for suggestion in result["suggestions"]:
        for lineup in suggestion["lineups"]:
            assert lineup["role_scenario_sensitivity"] is None
