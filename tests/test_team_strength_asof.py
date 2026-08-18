from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from fpl_model.validation.team_strength_asof import team_strength_as_of


def _team_fixture_frame() -> pd.DataFrame:
    # 2 teams, 4 GWs each: team_xg_for/team_xg_against chosen for easy hand math.
    rows = []
    for gw in range(1, 5):
        rows.append(
            {
                "team": "Home",
                "fixture_id": gw,
                "gameweek": gw,
                "team_xg_for": 1.0 + 0.1 * gw,
                "team_xg_against": 1.0,
            }
        )
        rows.append(
            {
                "team": "Away",
                "fixture_id": gw,
                "gameweek": gw,
                "team_xg_for": 2.0,
                "team_xg_against": 1.5,
            }
        )
    return pd.DataFrame(rows)


def test_team_strength_as_of_uses_only_prior_gameweeks():
    frame = _team_fixture_frame()

    result = team_strength_as_of(frame, as_of_gameweek=3, short_form_gameweeks=6)

    assert set(result) == {"Home", "Away"}
    home = result["Home"]
    # GW < 3 => GW1, GW2 only; both fully inside the 6-GW short-form window too.
    assert home.matches_played == 2
    assert home.short_form_matches_played == 2
    # Long-form xG rate = mean(1.1, 1.2) = 1.15; long == short here (same window).
    assert home.strength.opponent_xg_per_match == pytest.approx(1.15)


def test_team_strength_as_of_short_form_window_is_trailing():
    frame = _team_fixture_frame()

    result = team_strength_as_of(frame, as_of_gameweek=4, short_form_gameweeks=2)

    home = result["Home"]
    # GW < 4 => GW1-3 long form (3 matches); short form trailing 2 GW => GW2-3.
    assert home.matches_played == 3
    assert home.short_form_matches_played == 2
    assert "SHORT_FORM_WINDOW_TRUNCATED" not in home.data_quality_flags


def test_team_strength_as_of_flags_truncated_short_form_window():
    frame = _team_fixture_frame()

    # as_of_gameweek=2 => only GW1 exists; short-form window (6 GW) is truncated.
    result = team_strength_as_of(frame, as_of_gameweek=2, short_form_gameweeks=6)

    home = result["Home"]
    assert home.matches_played == 1
    assert home.short_form_matches_played == 1
    assert "SHORT_FORM_WINDOW_TRUNCATED" in home.data_quality_flags


def test_team_strength_as_of_omits_team_with_no_prior_matches():
    frame = pd.DataFrame(
        [
            {"team": "Home", "fixture_id": 5, "gameweek": 5, "team_xg_for": 1.0, "team_xg_against": 1.0},
        ]
    )

    result = team_strength_as_of(frame, as_of_gameweek=3)

    assert result == {}


def test_team_strength_as_of_is_causally_unaffected_by_future_gameweeks():
    frame = _team_fixture_frame()
    before = team_strength_as_of(frame, as_of_gameweek=3, short_form_gameweeks=6)

    mutated = frame.copy()
    mutated.loc[mutated["gameweek"] >= 3, "team_xg_for"] = 99.0
    mutated.loc[mutated["gameweek"] >= 3, "team_xg_against"] = 99.0
    after = team_strength_as_of(mutated, as_of_gameweek=3, short_form_gameweeks=6)

    assert before == after


def test_team_strength_as_of_rejects_out_of_range_gameweek():
    frame = _team_fixture_frame()
    with pytest.raises(ValueError, match="as_of_gameweek"):
        team_strength_as_of(frame, as_of_gameweek=0)


def _team_fixture_frame_with_kickoffs() -> pd.DataFrame:
    kickoffs = {
        1: datetime(2025, 8, 16, 14, 0, tzinfo=UTC),
        2: datetime(2025, 8, 23, 14, 0, tzinfo=UTC),
        3: datetime(2025, 8, 30, 14, 0, tzinfo=UTC),
        4: datetime(2025, 9, 13, 14, 0, tzinfo=UTC),
    }
    frame = _team_fixture_frame()
    frame["kickoff_time"] = frame["gameweek"].map(kickoffs)
    return frame


def test_team_strength_as_of_excludes_postponed_fixture_via_target_deadline():
    # Postpone GW2's rows to kick off after GW4's kickoff, i.e. after any
    # reasonable GW3 deadline. gameweek < 3 alone would still include it.
    frame = _team_fixture_frame_with_kickoffs()
    frame.loc[frame["gameweek"] == 2, "kickoff_time"] = datetime(
        2025, 9, 1, 14, 0, tzinfo=UTC
    )
    target_deadline = datetime(2025, 8, 30, 12, 30, tzinfo=UTC)  # GW3 kickoff - 90min

    without_gate = team_strength_as_of(frame, as_of_gameweek=3, short_form_gameweeks=6)
    with_gate = team_strength_as_of(
        frame,
        as_of_gameweek=3,
        short_form_gameweeks=6,
        target_deadline=target_deadline,
    )

    assert without_gate["Home"].matches_played == 2  # GW1 + postponed GW2
    assert with_gate["Home"].matches_played == 1  # GW1 only
    assert with_gate["Home"].strength.opponent_xg_per_match == pytest.approx(1.1)


def test_team_strength_as_of_rejects_naive_target_deadline():
    frame = _team_fixture_frame_with_kickoffs()
    with pytest.raises(ValueError, match="target_deadline"):
        team_strength_as_of(
            frame,
            as_of_gameweek=3,
            target_deadline=datetime(2025, 8, 30, 12, 30),  # naive
        )
