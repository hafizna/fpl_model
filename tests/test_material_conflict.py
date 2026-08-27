from __future__ import annotations

import pytest

from fpl_model.validation.material_conflict import (
    HIGH_APPEARANCE_PROBABILITY_THRESHOLD,
    LOW_START_PROBABILITY_THRESHOLD,
    SUBSTANTIAL_START_MINUTES,
    UNEXPECTED_BLANK,
    UNEXPECTED_SUBSTANTIAL_START,
    MaterialConflict,
    detect_conflict,
)


def test_unexpected_substantial_start_when_low_projection_but_started():
    conflict = detect_conflict(
        projected_start_probability=0.1,
        projected_appearance_probability=0.15,
        actual_minutes=75,
        actual_started=True,
    )

    assert conflict == UNEXPECTED_SUBSTANTIAL_START


def test_no_conflict_when_substantial_start_matches_high_projection():
    conflict = detect_conflict(
        projected_start_probability=0.9,
        projected_appearance_probability=0.95,
        actual_minutes=90,
        actual_started=True,
    )

    assert conflict is None


def test_substantial_start_boundary_is_inclusive_at_60_minutes():
    conflict = detect_conflict(
        projected_start_probability=0.1,
        projected_appearance_probability=0.15,
        actual_minutes=SUBSTANTIAL_START_MINUTES,
        actual_started=True,
    )

    assert conflict == UNEXPECTED_SUBSTANTIAL_START


def test_no_conflict_just_below_substantial_start_threshold():
    conflict = detect_conflict(
        projected_start_probability=0.05,
        projected_appearance_probability=0.05,
        actual_minutes=SUBSTANTIAL_START_MINUTES - 1,
        actual_started=False,
    )

    assert conflict is None


def test_unexpected_blank_when_high_projection_but_zero_minutes():
    conflict = detect_conflict(
        projected_start_probability=0.8,
        projected_appearance_probability=0.9,
        actual_minutes=0,
        actual_started=False,
    )

    assert conflict == UNEXPECTED_BLANK


def test_no_conflict_when_blank_matches_low_projection():
    conflict = detect_conflict(
        projected_start_probability=0.05,
        projected_appearance_probability=0.1,
        actual_minutes=0,
        actual_started=False,
    )

    assert conflict is None


def test_blank_conflict_boundary_is_inclusive_at_high_threshold():
    conflict = detect_conflict(
        projected_start_probability=HIGH_APPEARANCE_PROBABILITY_THRESHOLD,
        projected_appearance_probability=HIGH_APPEARANCE_PROBABILITY_THRESHOLD,
        actual_minutes=0,
        actual_started=False,
    )

    assert conflict == UNEXPECTED_BLANK


def test_no_blank_conflict_just_below_high_threshold():
    conflict = detect_conflict(
        projected_start_probability=HIGH_APPEARANCE_PROBABILITY_THRESHOLD - 0.01,
        projected_appearance_probability=HIGH_APPEARANCE_PROBABILITY_THRESHOLD - 0.01,
        actual_minutes=0,
        actual_started=False,
    )

    assert conflict is None


def test_no_conflict_for_ordinary_cameo_minutes():
    # A 20-minute substitute appearance is neither a "substantial start" nor
    # a "blank" -- it must not trigger either conflict shape regardless of
    # the projection.
    conflict = detect_conflict(
        projected_start_probability=0.9,
        projected_appearance_probability=0.95,
        actual_minutes=20,
        actual_started=False,
    )

    assert conflict is None

    conflict_low = detect_conflict(
        projected_start_probability=0.05,
        projected_appearance_probability=0.05,
        actual_minutes=20,
        actual_started=False,
    )

    assert conflict_low is None


def test_substantial_start_boundary_matches_low_start_probability_threshold():
    conflict = detect_conflict(
        projected_start_probability=LOW_START_PROBABILITY_THRESHOLD,
        projected_appearance_probability=LOW_START_PROBABILITY_THRESHOLD,
        actual_minutes=90,
        actual_started=True,
    )

    # At exactly the threshold, projected_start_probability is no longer
    # "< threshold", so this must NOT be flagged.
    assert conflict is None


def test_material_conflict_rejects_unknown_conflict_type():
    with pytest.raises(ValueError, match="unknown conflict_type"):
        MaterialConflict(
            fpl_id=1,
            conflict_type="made_up",
            projected_start_probability=0.1,
            projected_appearance_probability=0.1,
            actual_minutes=90,
            actual_started=True,
            reason="test",
        )
