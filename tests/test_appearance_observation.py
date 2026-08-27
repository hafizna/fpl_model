from __future__ import annotations

import pytest

from fpl_model.validation.appearance_observation import (
    NO_TEAM_FIXTURE,
    NOT_YET_ELIGIBLE,
    STARTER,
    SUBSTITUTE,
    UNAVAILABLE,
    UNUSED_SUBSTITUTE_OR_NOT_IN_SQUAD,
    AppearanceObservationResult,
    appearance_observation_report,
    derive_appearance_observation,
)


def _derive(**overrides):
    arguments = {
        "minutes": 0,
        "starts": 0,
        "is_eligible": True,
        "is_registered": True,
        "team_has_fixture": True,
    }
    arguments.update(overrides)
    return derive_appearance_observation(**arguments)


def test_started_and_played_is_a_starter_regardless_of_minutes():
    result = _derive(minutes=90, starts=1)
    assert result.observation == STARTER
    assert "90 minutes" in result.reason


def test_dgw_two_starts_is_still_a_starter():
    result = _derive(minutes=180, starts=2)
    assert result.observation == STARTER


def test_zero_starts_positive_minutes_is_a_substitute():
    result = _derive(minutes=12, starts=0)
    assert result.observation == SUBSTITUTE
    assert "12 minutes" in result.reason


def test_zero_minutes_not_registered_is_not_yet_eligible_even_if_eligible_flag_true():
    result = _derive(minutes=0, starts=0, is_registered=False, is_eligible=True)
    assert result.observation == NOT_YET_ELIGIBLE


def test_zero_minutes_resolved_ineligible_is_unavailable():
    result = _derive(minutes=0, starts=0, is_eligible=False)
    assert result.observation == UNAVAILABLE


def test_not_registered_takes_precedence_over_resolved_ineligible():
    # A player who is not registered at all cannot simultaneously have a
    # meaningful eligibility resolution -- registration is checked first.
    result = _derive(minutes=0, starts=0, is_registered=False, is_eligible=False)
    assert result.observation == NOT_YET_ELIGIBLE


def test_zero_minutes_no_team_fixture_is_no_team_fixture():
    result = _derive(minutes=0, starts=0, is_eligible=True, team_has_fixture=False)
    assert result.observation == NO_TEAM_FIXTURE


def test_no_team_fixture_only_applies_once_eligibility_is_clear():
    # An ineligible player whose team also has no fixture is still reported
    # as UNAVAILABLE -- the resolved eligibility block is the more specific,
    # decision-relevant reason.
    result = _derive(minutes=0, starts=0, is_eligible=False, team_has_fixture=False)
    assert result.observation == UNAVAILABLE


def test_zero_minutes_no_block_and_a_fixture_is_the_named_ambiguous_bucket():
    result = _derive(minutes=0, starts=0, is_eligible=True, team_has_fixture=True)
    assert result.observation == UNUSED_SUBSTITUTE_OR_NOT_IN_SQUAD
    assert "does not expose" in result.reason


def test_unresolved_eligibility_is_treated_like_no_block():
    result = _derive(minutes=0, starts=0, is_eligible=None, team_has_fixture=True)
    assert result.observation == UNUSED_SUBSTITUTE_OR_NOT_IN_SQUAD


def test_negative_minutes_or_starts_is_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        _derive(minutes=-1)
    with pytest.raises(ValueError, match="non-negative"):
        _derive(starts=-1)


def test_result_rejects_unknown_observation():
    with pytest.raises(ValueError, match="unknown observation"):
        AppearanceObservationResult(observation="benched", reason="x")


def test_result_rejects_blank_reason():
    with pytest.raises(ValueError, match="reason must not be blank"):
        AppearanceObservationResult(observation=STARTER, reason="   ")


def test_report_serialises_and_handles_none():
    result = _derive(minutes=90, starts=1)
    report = appearance_observation_report(result)
    assert report == {"observation": STARTER, "reason": result.reason}
    assert appearance_observation_report(None) is None
