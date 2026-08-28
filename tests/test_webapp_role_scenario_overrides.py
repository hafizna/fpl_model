"""Coverage for webapp.service's reviewed role-scenario xPts override.

Verifies the base release/catalog is never mutated, that an override
actually changes the recomputed lineup/transfer recommendation, and that
out-of-horizon or unknown-player overrides are rejected rather than silently
ignored.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fpl_model.webapp.service import (
    RoleScenarioOverride,
    apply_role_scenario_overrides,
    load_release_catalog,
    recommend_web_lineups,
    recommend_web_transfers,
)
from tests.test_webapp_service import _release_file


def test_apply_role_scenario_overrides_does_not_mutate_the_input(tmp_path: Path):
    release_path = tmp_path / "release.json"
    fpl_ids = _release_file(release_path)
    horizon, _catalog, projections, _health, _release_id = load_release_catalog(release_path)
    original_xpts = projections[2][fpl_ids[0]].expected_points

    overridden = apply_role_scenario_overrides(
        projections,
        (RoleScenarioOverride(fpl_id=fpl_ids[0], gameweek=2, xpts=0.0),),
        horizon=horizon,
    )

    assert overridden[2][fpl_ids[0]].expected_points == 0.0
    assert projections[2][fpl_ids[0]].expected_points == original_xpts  # unchanged


def test_apply_role_scenario_overrides_rejects_a_gameweek_outside_the_horizon(tmp_path: Path):
    release_path = tmp_path / "release.json"
    fpl_ids = _release_file(release_path)
    horizon, _catalog, projections, _health, _release_id = load_release_catalog(release_path)

    # GW1 is a valid Gameweek in general but outside this fixture's own
    # GW2-4 horizon (see _release_file).
    with pytest.raises(ValueError, match="outside this release's horizon"):
        apply_role_scenario_overrides(
            projections,
            (RoleScenarioOverride(fpl_id=fpl_ids[0], gameweek=1, xpts=0.0),),
            horizon=horizon,
        )


def test_apply_role_scenario_overrides_rejects_an_unknown_player(tmp_path: Path):
    release_path = tmp_path / "release.json"
    _release_file(release_path)
    horizon, _catalog, projections, _health, _release_id = load_release_catalog(release_path)

    with pytest.raises(ValueError, match="no GW2 projection"):
        apply_role_scenario_overrides(
            projections,
            (RoleScenarioOverride(fpl_id=99999, gameweek=2, xpts=0.0),),
            horizon=horizon,
        )


def test_role_scenario_override_rejects_negative_xpts():
    with pytest.raises(ValueError, match="non-negative"):
        RoleScenarioOverride(fpl_id=1, gameweek=2, xpts=-1.0)


def test_recommend_web_lineups_recomputes_from_an_override(tmp_path: Path):
    release_path = tmp_path / "release.json"
    fpl_ids = _release_file(release_path)
    # fpl_id 11 is the weakest starter in this fixture (see test_lineup.py's
    # own _projections convention, mirrored by _release_file: xpts == fpl_id / 2).
    baseline = recommend_web_lineups(fpl_ids, release_path=release_path)
    assert baseline["is_reviewed_scenario"] is False
    baseline_starters = {row["fpl_id"] for row in baseline["lineups"][0]["starters"]}
    assert 11 in baseline_starters

    scenario = recommend_web_lineups(
        fpl_ids,
        role_scenario_overrides=(RoleScenarioOverride(fpl_id=11, gameweek=2, xpts=0.0),),
        release_path=release_path,
    )

    assert scenario["is_reviewed_scenario"] is True
    scenario_starters = {row["fpl_id"] for row in scenario["lineups"][0]["starters"]}
    # Blanking the weakest starter (fpl_id 11, MID) must swap them out for
    # the only other MID on the bench in this fixture, fpl_id 8.
    assert 11 not in scenario_starters
    assert 8 in scenario_starters
    # Only GW2 was overridden -- GW3/GW4 must be untouched.
    assert scenario["lineups"][1]["total_xpts"] == baseline["lineups"][1]["total_xpts"]


def test_recommend_web_transfers_recomputes_the_baseline_from_an_override(tmp_path: Path):
    release_path = tmp_path / "release.json"
    fpl_ids = _release_file(release_path)

    baseline = recommend_web_transfers(fpl_ids, release_path=release_path)
    scenario = recommend_web_transfers(
        fpl_ids,
        role_scenario_overrides=(RoleScenarioOverride(fpl_id=11, gameweek=2, xpts=0.0),),
        release_path=release_path,
    )

    assert baseline["is_reviewed_scenario"] is False
    assert scenario["is_reviewed_scenario"] is True
    assert scenario["baseline_cumulative_xpts"] < baseline["baseline_cumulative_xpts"]
