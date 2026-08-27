from __future__ import annotations

import pytest

from fpl_model.validation.role_state import (
    LIKELY_BENCH,
    LIKELY_STARTER,
    ROTATION,
    UNAVAILABLE,
    UNKNOWN,
    RoleStateResult,
    derive_role_state,
)


def test_unavailable_when_resolved_ineligible():
    result = derive_role_state(
        is_eligible=False, start_probability=0.9, appearance_probability=0.95
    )

    assert result.role_state == UNAVAILABLE
    assert "unavailable" in result.reason.lower()


def test_unavailable_takes_precedence_over_high_start_probability():
    # A player can have a stale high start_probability from before a late
    # injury was resolved -- eligibility must win.
    result = derive_role_state(
        is_eligible=False, start_probability=0.99, appearance_probability=0.99
    )

    assert result.role_state == UNAVAILABLE


def test_unknown_when_eligibility_unresolved():
    result = derive_role_state(
        is_eligible=None, start_probability=0.9, appearance_probability=0.95
    )

    assert result.role_state == UNKNOWN


def test_unknown_when_start_probability_missing():
    result = derive_role_state(
        is_eligible=True, start_probability=None, appearance_probability=0.5
    )

    assert result.role_state == UNKNOWN


def test_unknown_when_appearance_probability_missing():
    result = derive_role_state(
        is_eligible=True, start_probability=0.5, appearance_probability=None
    )

    assert result.role_state == UNKNOWN


def test_likely_starter_at_high_start_probability():
    result = derive_role_state(
        is_eligible=True, start_probability=0.9, appearance_probability=0.95
    )

    assert result.role_state == LIKELY_STARTER
    assert "90%" in result.reason


def test_likely_starter_boundary_is_inclusive():
    result = derive_role_state(
        is_eligible=True, start_probability=0.75, appearance_probability=0.8
    )

    assert result.role_state == LIKELY_STARTER


def test_rotation_for_mid_range_start_probability():
    result = derive_role_state(
        is_eligible=True, start_probability=0.5, appearance_probability=0.55
    )

    assert result.role_state == ROTATION


def test_rotation_for_impact_substitute_with_low_start_but_high_appearance():
    # Rarely starts, but frequently used as a substitute -- should not be
    # collapsed into LIKELY_BENCH just because start_probability is low.
    result = derive_role_state(
        is_eligible=True, start_probability=0.1, appearance_probability=0.6
    )

    assert result.role_state == ROTATION


def test_likely_bench_when_both_probabilities_are_low():
    result = derive_role_state(
        is_eligible=True, start_probability=0.05, appearance_probability=0.1
    )

    assert result.role_state == LIKELY_BENCH


def test_likely_bench_boundary_is_exclusive_at_rotation_threshold():
    result = derive_role_state(
        is_eligible=True, start_probability=0.29, appearance_probability=0.29
    )

    assert result.role_state == LIKELY_BENCH


def test_rotation_boundary_is_inclusive_at_rotation_threshold():
    result = derive_role_state(
        is_eligible=True, start_probability=0.30, appearance_probability=0.10
    )

    assert result.role_state == ROTATION


def test_every_role_state_reason_is_nonblank_and_valid():
    cases = [
        (False, 0.5, 0.5),
        (None, 0.5, 0.5),
        (True, None, 0.5),
        (True, 0.5, None),
        (True, 0.9, 0.95),
        (True, 0.5, 0.5),
        (True, 0.05, 0.05),
    ]
    for is_eligible, start_probability, appearance_probability in cases:
        result = derive_role_state(
            is_eligible=is_eligible,
            start_probability=start_probability,
            appearance_probability=appearance_probability,
        )
        assert result.reason.strip()


def test_role_state_result_rejects_unknown_state():
    with pytest.raises(ValueError, match="unknown role_state"):
        RoleStateResult(role_state="made_up", reason="test")


def test_role_state_result_rejects_blank_reason():
    with pytest.raises(ValueError, match="must not be blank"):
        RoleStateResult(role_state=LIKELY_STARTER, reason="   ")
