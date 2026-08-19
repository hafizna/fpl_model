from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import duckdb
import pandas as pd
import pytest

from fpl_model.ingest.player_history import import_player_fixture_history
from fpl_model.model.appearance import AppearanceProjection
from fpl_model.validation.appearance_calibration import OlsFit
from fpl_model.validation.appearance_policy_backtest import (
    HIGH_END_START_PROBABILITY_THRESHOLD,
    POLICIES,
    AppearancePolicyBacktestGap,
    AppearancePolicyCalibrationRow,
    apply_calibration_policy,
    fit_for_gameweek,
    materialize_appearance_policy_backtest,
    rescale_appearance_projection,
    verify_raw_row_level_parity,
)
from fpl_model.validation.backtest import BacktestObservation, score_predictions
from fpl_model.validation.benchwarmers_backtest import (
    BacktestGapSummary,
    BenchwarmersBacktestResult,
    materialize_benchwarmers_walk_forward_backtest,
)

# ---------------------------------------------------------------------------
# rescale_appearance_projection
# ---------------------------------------------------------------------------


def _projection(
    *,
    start_probability: float,
    substitute_appearance_probability: float,
    sixty_minute_probability: float,
    expected_minutes: float = 60.0,
) -> AppearanceProjection:
    appearance_probability = start_probability + substitute_appearance_probability
    return AppearanceProjection(
        start_probability=start_probability,
        substitute_appearance_probability=substitute_appearance_probability,
        appearance_probability=appearance_probability,
        sixty_minute_probability=sixty_minute_probability,
        expected_minutes=expected_minutes,
        appearance_xpts=appearance_probability,
        sixty_minute_xpts=sixty_minute_probability,
        total_xpts=appearance_probability + sixty_minute_probability,
    )


_MEAN_MINUTES_PER_START = 85.0
_MEAN_MINUTES_PER_SUBSTITUTE = 20.0


def _rescale(raw: AppearanceProjection, *, calibrated_start_probability: float) -> AppearanceProjection:
    return rescale_appearance_projection(
        raw,
        calibrated_start_probability=calibrated_start_probability,
        mean_minutes_per_start=_MEAN_MINUTES_PER_START,
        mean_minutes_per_substitute=_MEAN_MINUTES_PER_SUBSTITUTE,
    )


def test_rescale_applies_the_same_ratio_to_every_dependent_field():
    raw = _projection(
        start_probability=0.9, substitute_appearance_probability=0.05, sixty_minute_probability=0.8
    )

    calibrated = _rescale(raw, calibrated_start_probability=0.7)

    ratio = 0.7 / 0.9
    assert calibrated.start_probability == pytest.approx(0.7)
    assert calibrated.substitute_appearance_probability == pytest.approx(0.05 * ratio)
    assert calibrated.sixty_minute_probability == pytest.approx(0.8 * ratio)
    assert calibrated.appearance_probability == pytest.approx(0.7 + 0.05 * ratio)
    assert calibrated.appearance_xpts == pytest.approx(calibrated.appearance_probability)
    assert calibrated.sixty_minute_xpts == pytest.approx(calibrated.sixty_minute_probability)
    assert calibrated.total_xpts == pytest.approx(
        calibrated.appearance_xpts + calibrated.sixty_minute_xpts
    )


def test_rescale_recomputes_expected_minutes_from_calibrated_probabilities():
    # Numbers chosen so the substitute-probability rescale does NOT hit its
    # clamp ceiling (naive_sub=0.07 < ceiling 1.0-0.7=0.3), isolating the
    # unclamped recomputation path -- the clamped path is covered by
    # test_rescale_expected_minutes_uses_the_clamped_substitute_probability.
    raw = _projection(
        start_probability=0.5,
        substitute_appearance_probability=0.05,
        sixty_minute_probability=0.3,
        expected_minutes=999.0,  # deliberately wrong raw value to prove it's not reused
    )

    calibrated = _rescale(raw, calibrated_start_probability=0.7)

    ratio = 0.7 / 0.5
    expected_substitute = 0.05 * ratio  # not clamped at this ratio
    assert calibrated.substitute_appearance_probability == pytest.approx(expected_substitute)
    expected_minutes = (
        0.7 * _MEAN_MINUTES_PER_START + expected_substitute * _MEAN_MINUTES_PER_SUBSTITUTE
    )
    assert calibrated.expected_minutes == pytest.approx(expected_minutes)
    assert calibrated.expected_minutes != 999.0


def test_rescale_expected_minutes_uses_the_clamped_substitute_probability():
    # When substitute_appearance_probability is clamped to its ceiling, the
    # recomputed expected_minutes must use the CLAMPED value, not the
    # naively-scaled one that would otherwise overshoot.
    raw = _projection(
        start_probability=0.3, substitute_appearance_probability=0.3, sixty_minute_probability=0.25
    )

    calibrated = _rescale(raw, calibrated_start_probability=0.95)

    clamped_substitute = 1.0 - 0.95
    assert calibrated.substitute_appearance_probability == pytest.approx(clamped_substitute, abs=1e-9)
    expected_minutes = 0.95 * _MEAN_MINUTES_PER_START + clamped_substitute * _MEAN_MINUTES_PER_SUBSTITUTE
    assert calibrated.expected_minutes == pytest.approx(expected_minutes)


def test_rescale_expected_minutes_matches_blend_conditional_appearance_formula():
    # Directly mirrors model.appearance.blend_conditional_appearance's own
    # expected_minutes formula: start_probability * minutes_per_start +
    # substitute_probability * minutes_per_substitute -- just fed the
    # calibrated/rescaled probabilities as the source values.
    raw = _projection(
        start_probability=0.6, substitute_appearance_probability=0.15, sixty_minute_probability=0.5
    )
    calibrated_start_probability = 0.75

    calibrated = _rescale(raw, calibrated_start_probability=calibrated_start_probability)

    expected = (
        calibrated_start_probability * _MEAN_MINUTES_PER_START
        + calibrated.substitute_appearance_probability * _MEAN_MINUTES_PER_SUBSTITUTE
    )
    assert calibrated.expected_minutes == pytest.approx(expected)


def test_rescale_clamps_substitute_probability_to_its_ceiling():
    # A large inflation ratio would naively push substitute probability past
    # 1.0 - calibrated_start_probability -- must be clamped, not overshoot.
    raw = _projection(
        start_probability=0.3, substitute_appearance_probability=0.3, sixty_minute_probability=0.25
    )

    calibrated = _rescale(raw, calibrated_start_probability=0.95)

    assert calibrated.substitute_appearance_probability == pytest.approx(
        1.0 - 0.95, abs=1e-9
    )
    assert calibrated.appearance_probability <= 1.0 + 1e-9


def test_rescale_clamps_sixty_minute_probability_to_appearance_probability_ceiling():
    raw = _projection(
        start_probability=0.2, substitute_appearance_probability=0.05, sixty_minute_probability=0.15
    )

    calibrated = _rescale(raw, calibrated_start_probability=0.99)

    assert calibrated.sixty_minute_probability <= calibrated.appearance_probability + 1e-9


def test_rescale_never_produces_negative_fields():
    raw = _projection(
        start_probability=0.9, substitute_appearance_probability=0.05, sixty_minute_probability=0.8
    )

    # Shrink toward zero -- ratio is tiny but positive; every field must stay >= 0.
    calibrated = _rescale(raw, calibrated_start_probability=0.001)

    assert calibrated.start_probability >= 0.0
    assert calibrated.substitute_appearance_probability >= 0.0
    assert calibrated.sixty_minute_probability >= 0.0
    assert calibrated.appearance_probability >= 0.0
    assert calibrated.expected_minutes >= 0.0


def test_rescale_rejects_out_of_range_calibrated_start_probability():
    raw = _projection(
        start_probability=0.5, substitute_appearance_probability=0.1, sixty_minute_probability=0.3
    )
    with pytest.raises(ValueError, match=r"must be clamped to \[0, 1\]"):
        _rescale(raw, calibrated_start_probability=1.2)


def test_rescale_rejects_zero_raw_start_probability():
    raw = _projection(
        start_probability=0.0, substitute_appearance_probability=0.0, sixty_minute_probability=0.0
    )
    with pytest.raises(ValueError, match="rescale ratio calibrated/raw is undefined"):
        _rescale(raw, calibrated_start_probability=0.3)


def test_rescale_identity_when_calibrated_equals_raw():
    raw = _projection(
        start_probability=0.6, substitute_appearance_probability=0.1, sixty_minute_probability=0.4
    )

    calibrated = _rescale(raw, calibrated_start_probability=0.6)

    assert calibrated.start_probability == pytest.approx(raw.start_probability)
    assert calibrated.substitute_appearance_probability == pytest.approx(
        raw.substitute_appearance_probability
    )
    assert calibrated.sixty_minute_probability == pytest.approx(raw.sixty_minute_probability)
    expected = 0.6 * _MEAN_MINUTES_PER_START + 0.1 * _MEAN_MINUTES_PER_SUBSTITUTE
    assert calibrated.expected_minutes == pytest.approx(expected)


# ---------------------------------------------------------------------------
# apply_calibration_policy
# ---------------------------------------------------------------------------


_FIT = OlsFit(slope=0.8, intercept=0.05, training_rows=100, training_gameweeks=10)


def _apply(raw, *, policy, fit):
    return apply_calibration_policy(
        raw,
        policy=policy,
        fit=fit,
        mean_minutes_per_start=_MEAN_MINUTES_PER_START,
        mean_minutes_per_substitute=_MEAN_MINUTES_PER_SUBSTITUTE,
    )


def test_raw_policy_returns_the_input_unchanged_even_without_a_fit():
    raw = _projection(
        start_probability=0.5, substitute_appearance_probability=0.1, sixty_minute_probability=0.3
    )

    result = _apply(raw, policy="raw", fit=None)

    assert result is raw


def test_global_policy_calibrates_every_row_regardless_of_band():
    low = _projection(
        start_probability=0.3, substitute_appearance_probability=0.2, sixty_minute_probability=0.1
    )
    high = _projection(
        start_probability=0.9, substitute_appearance_probability=0.05, sixty_minute_probability=0.8
    )

    low_result = _apply(low, policy="global", fit=_FIT)
    high_result = _apply(high, policy="global", fit=_FIT)

    assert low_result.start_probability == pytest.approx(_FIT.intercept + _FIT.slope * 0.3)
    assert high_result.start_probability == pytest.approx(_FIT.intercept + _FIT.slope * 0.9)


def test_high_end_shrinkage_leaves_rows_below_threshold_unchanged():
    below = _projection(
        start_probability=HIGH_END_START_PROBABILITY_THRESHOLD - 0.01,
        substitute_appearance_probability=0.1,
        sixty_minute_probability=0.3,
    )

    result = _apply(below, policy="high_end_shrinkage", fit=_FIT)

    assert result is below  # unchanged object, not merely equal value


def test_high_end_shrinkage_calibrates_rows_at_or_above_threshold():
    at_threshold = _projection(
        start_probability=HIGH_END_START_PROBABILITY_THRESHOLD,
        substitute_appearance_probability=0.05,
        sixty_minute_probability=0.7,
    )

    result = _apply(at_threshold, policy="high_end_shrinkage", fit=_FIT)

    expected = _FIT.intercept + _FIT.slope * HIGH_END_START_PROBABILITY_THRESHOLD
    assert result.start_probability == pytest.approx(expected)


def test_high_end_shrinkage_boundary_is_inclusive_of_the_threshold_itself():
    just_below = _projection(
        start_probability=HIGH_END_START_PROBABILITY_THRESHOLD - 1e-9,
        substitute_appearance_probability=0.1,
        sixty_minute_probability=0.3,
    )
    at = _projection(
        start_probability=HIGH_END_START_PROBABILITY_THRESHOLD,
        substitute_appearance_probability=0.1,
        sixty_minute_probability=0.3,
    )

    assert _apply(just_below, policy="high_end_shrinkage", fit=_FIT) is just_below
    assert (
        _apply(at, policy="high_end_shrinkage", fit=_FIT).start_probability
        != HIGH_END_START_PROBABILITY_THRESHOLD
    )


def test_global_and_high_end_shrinkage_reject_missing_fit():
    raw = _projection(
        start_probability=0.5, substitute_appearance_probability=0.1, sixty_minute_probability=0.3
    )
    with pytest.raises(ValueError, match="fit must not be None"):
        _apply(raw, policy="global", fit=None)
    with pytest.raises(ValueError, match="fit must not be None"):
        _apply(raw, policy="high_end_shrinkage", fit=None)


def test_calibrated_start_probability_is_clamped_before_rescaling():
    # A fit whose linear extrapolation would push the calibrated value
    # outside [0, 1] must be clamped, not passed through raw to
    # rescale_appearance_projection (which would raise).
    extreme_fit = OlsFit(slope=2.0, intercept=0.5, training_rows=50, training_gameweeks=6)
    raw = _projection(
        start_probability=0.9, substitute_appearance_probability=0.05, sixty_minute_probability=0.8
    )

    result = _apply(raw, policy="global", fit=extreme_fit)

    assert result.start_probability == pytest.approx(1.0)  # clamped, not 0.5 + 2.0*0.9 = 2.3


def test_apply_calibration_policy_skips_zero_raw_start_probability_rows():
    zero = _projection(
        start_probability=0.0, substitute_appearance_probability=0.0, sixty_minute_probability=0.0
    )

    result = _apply(zero, policy="global", fit=_FIT)

    assert result is zero  # nothing to rescale against; returned unchanged, not raised


def test_apply_calibration_policy_rejects_unknown_policy():
    raw = _projection(
        start_probability=0.5, substitute_appearance_probability=0.1, sixty_minute_probability=0.3
    )
    with pytest.raises(ValueError, match="unknown policy"):
        _apply(raw, policy="nonexistent", fit=_FIT)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# fit_for_gameweek: no-lookahead + deadline safety
# ---------------------------------------------------------------------------

_GW_DEADLINE = {
    gw: datetime(2025, 8, 16, 12, 30, tzinfo=UTC) + timedelta(days=7 * (gw - 1)) for gw in range(1, 15)
}


def _calibration_row(
    gameweek: int,
    predicted: float,
    started: bool,
    *,
    outcome_available_at: datetime | None = None,
) -> AppearancePolicyCalibrationRow:
    # Default: outcome available well before its own gameweek's deadline
    # (a normal, non-postponed fixture) -- callers exercising the
    # deadline-safety filter pass an explicit outcome_available_at instead.
    if outcome_available_at is None:
        outcome_available_at = _GW_DEADLINE[gameweek] - timedelta(hours=1)
    return AppearancePolicyCalibrationRow(
        gameweek=gameweek,
        predicted_start_probability=predicted,
        actual_started=started,
        outcome_available_at=outcome_available_at,
    )


def test_fit_for_gameweek_returns_none_below_minimum_prior_gameweeks():
    rows_by_gw = {
        1: [_calibration_row(1, 0.5, True), _calibration_row(1, 0.3, False)],
        2: [_calibration_row(2, 0.6, True), _calibration_row(2, 0.4, False)],
    }

    fit = fit_for_gameweek(
        rows_by_gw, gameweek=3, target_deadline=_GW_DEADLINE[3], minimum_calibration_gameweeks=3
    )

    assert fit is None


def test_fit_for_gameweek_uses_only_strictly_prior_gameweeks():
    rows_by_gw = {
        1: [_calibration_row(1, 0.2, False), _calibration_row(1, 0.8, True)],
        2: [_calibration_row(2, 0.3, False), _calibration_row(2, 0.7, True)],
        # Gameweek 3's own rows must NEVER influence gameweek 3's fit.
        3: [_calibration_row(3, 0.99, False), _calibration_row(3, 0.01, True)],
    }

    fit = fit_for_gameweek(
        rows_by_gw, gameweek=3, target_deadline=_GW_DEADLINE[3], minimum_calibration_gameweeks=2
    )

    assert fit is not None
    assert fit.training_rows == 4  # only GW1+GW2's rows
    assert fit.training_gameweeks == 2


def test_fit_for_gameweek_current_or_future_gameweek_rows_cannot_change_an_earlier_folds_fit():
    baseline_rows_by_gw = {
        1: [_calibration_row(1, 0.2, False), _calibration_row(1, 0.8, True)],
        2: [_calibration_row(2, 0.3, False), _calibration_row(2, 0.7, True)],
    }
    baseline_fit = fit_for_gameweek(
        baseline_rows_by_gw, gameweek=3, target_deadline=_GW_DEADLINE[3], minimum_calibration_gameweeks=2
    )

    perturbed_rows_by_gw = dict(baseline_rows_by_gw)
    perturbed_rows_by_gw[3] = [_calibration_row(3, 1.0, True)] * 1000  # a future gameweek, wildly different

    perturbed_fit = fit_for_gameweek(
        perturbed_rows_by_gw, gameweek=3, target_deadline=_GW_DEADLINE[3], minimum_calibration_gameweeks=2
    )

    assert perturbed_fit == baseline_fit


def test_fit_for_gameweek_returns_none_for_degenerate_prior_predictor():
    rows_by_gw = {
        1: [_calibration_row(1, 0.5, True), _calibration_row(1, 0.5, False)],
        2: [_calibration_row(2, 0.5, True), _calibration_row(2, 0.5, False)],
    }

    fit = fit_for_gameweek(
        rows_by_gw, gameweek=3, target_deadline=_GW_DEADLINE[3], minimum_calibration_gameweeks=2
    )

    assert fit is None  # constant predicted_start_probability -- slope undefined


def test_fit_for_gameweek_excludes_a_row_whose_outcome_is_not_yet_available():
    # GW2's row is labelled GW2 (< target gameweek 4) but its OUTCOME was not
    # available until after GW4's own deadline (e.g. a fixture postponed
    # from GW2 into a later kickoff) -- must be excluded from GW4's fit even
    # though its gameweek LABEL alone would satisfy `gameweek < target`.
    rows_by_gw = {
        1: [_calibration_row(1, 0.2, False), _calibration_row(1, 0.8, True)],
        2: [
            _calibration_row(
                2, 0.99, False, outcome_available_at=_GW_DEADLINE[4] + timedelta(hours=1)
            )
        ],
        3: [_calibration_row(3, 0.3, False), _calibration_row(3, 0.7, True)],
    }

    fit = fit_for_gameweek(
        rows_by_gw, gameweek=4, target_deadline=_GW_DEADLINE[4], minimum_calibration_gameweeks=2
    )

    assert fit is not None
    # Only GW1 (2 rows) + GW3 (2 rows) contribute -- GW2's late-available row
    # is excluded, so training_rows must be 4, not 5.
    assert fit.training_rows == 4
    assert fit.training_gameweeks == 2


def test_fit_for_gameweek_counts_minimum_gameweeks_only_after_availability_filter():
    # GW2 contributes a row by LABEL, but that row's own outcome is not yet
    # available by GW4's deadline -- GW2 must NOT count toward the distinct
    # gameweek minimum, even though it has an entry in the input dict.
    rows_by_gw = {
        1: [_calibration_row(1, 0.2, False), _calibration_row(1, 0.8, True)],
        2: [
            _calibration_row(
                2, 0.99, False, outcome_available_at=_GW_DEADLINE[4] + timedelta(hours=1)
            )
        ],
    }

    # minimum_calibration_gameweeks=2 would be satisfied by {GW1, GW2} if
    # counted naively (2 keys in the dict), but GW2 has zero AVAILABLE rows
    # -- only GW1 counts, so this must be ineligible.
    fit = fit_for_gameweek(
        rows_by_gw, gameweek=4, target_deadline=_GW_DEADLINE[4], minimum_calibration_gameweeks=2
    )

    assert fit is None


def test_fit_for_gameweek_postponed_outcome_becoming_available_does_not_alter_an_earlier_fit():
    # Regression test (helper level): a postponed GW2 fixture whose outcome
    # only becomes available after GW4's deadline must not change GW4's own
    # fit -- mutating that outcome (e.g. once it's later known) must leave
    # an EARLIER gameweek's already-computed fit untouched, since a fit is
    # recomputed fresh at each step from that step's own deadline, never
    # retroactively.
    baseline_rows_by_gw = {
        1: [_calibration_row(1, 0.2, False), _calibration_row(1, 0.8, True)],
        2: [
            _calibration_row(
                2, 0.5, True, outcome_available_at=_GW_DEADLINE[4] + timedelta(hours=1)
            )
        ],
        3: [_calibration_row(3, 0.3, False), _calibration_row(3, 0.7, True)],
    }
    baseline_fit = fit_for_gameweek(
        baseline_rows_by_gw, gameweek=4, target_deadline=_GW_DEADLINE[4], minimum_calibration_gameweeks=2
    )

    # Mutate the postponed row's outcome (actual_started flips) -- since it
    # is still not AVAILABLE by GW4's deadline, this must not change GW4's
    # own fit at all.
    mutated_rows_by_gw = dict(baseline_rows_by_gw)
    mutated_rows_by_gw[2] = [
        _calibration_row(
            2, 0.5, False, outcome_available_at=_GW_DEADLINE[4] + timedelta(hours=1)
        )
    ]
    mutated_fit = fit_for_gameweek(
        mutated_rows_by_gw, gameweek=4, target_deadline=_GW_DEADLINE[4], minimum_calibration_gameweeks=2
    )

    assert mutated_fit == baseline_fit


# ---------------------------------------------------------------------------
# materialize_appearance_policy_backtest: full pipeline integration
# ---------------------------------------------------------------------------

TEAMS = {"A": 1, "B": 2, "C": 3, "D": 4}
PLAYERS = [
    (5001, "A", "DEF", 1),
    (5002, "B", "DEF", 2),
    (5003, "C", "DEF", 3),
    (5004, "D", "DEF", 4),
    (5005, "A", "GK", 5),
]
_SEASON_TOTAL_COLUMNS = (
    "minutes", "starts", "expected_goals", "expected_assists", "saves",
    "yellow_cards", "red_cards", "bonus", "bps", "defensive_contribution",
)


def _players_raw(gameweeks: pd.DataFrame) -> pd.DataFrame:
    totals = gameweeks.groupby("code")[list(_SEASON_TOTAL_COLUMNS)].sum()
    rows = []
    for code, team, position, element in PLAYERS:
        element_type = {"GK": 1, "DEF": 2, "MID": 3, "FWD": 4}[position]
        row = {
            "id": element, "code": code, "web_name": f"Player{code}",
            "team": TEAMS[team], "element_type": element_type,
        }
        row.update(totals.loc[code].to_dict())
        rows.append(row)
    return pd.DataFrame(rows)


def _gameweeks(
    *,
    num_gameweeks: int = 8,
    vary_starts: bool = False,
    postpone_player_5001_gw: int | None = None,
    postponed_player_actual_started: bool | None = None,
) -> pd.DataFrame:
    """Build synthetic gameweek rows.

    ``vary_starts=True`` alternates one player (5001) between starting and
    not appearing at all every other gameweek, and gives every OTHER player
    a fixed but non-1.0/0.0 start rate implicitly via always starting --
    enough real variance in realised ``starts`` outcomes for a walk-forward
    start_probability calibration fit to be non-degenerate (``vary_starts=
    False`` keeps every player always-starting, matching the byte-identity
    fixture used elsewhere in this file, where no fit is ever eligible).

    ``postpone_player_5001_gw`` moves player 5001's fixture in that
    gameweek to kick off much later (still labelled with the SAME GW
    number) -- simulating a postponed fixture whose gameweek LABEL is
    earlier than when its outcome actually becomes known.

    ``postponed_player_actual_started`` (only meaningful together with
    ``postpone_player_5001_gw``) overrides player 5001's realised
    ``starts``/``minutes``/``total_points`` outcome IN THE POSTPONED
    GAMEWEEK ONLY, independent of the ``vary_starts`` alternation rule --
    letting two otherwise-byte-identical fixtures differ in EXACTLY that one
    row's realised outcome, for a true mutation-invariance test (every other
    row, including every other gameweek's data for player 5001, is
    unaffected).
    """
    rows = []
    fixture_id = 100
    base_kickoff = datetime(2025, 8, 16, 14, 0, tzinfo=UTC)
    for gw in range(1, num_gameweeks + 1):
        kickoff_dt = base_kickoff + timedelta(weeks=gw - 1)
        for (home, away) in (("A", "B"), ("C", "D")):
            fixture_id += 1
            for team, was_home, opponent in ((home, True, TEAMS[away]), (away, False, TEAMS[home])):
                for code, player_team, position, element in PLAYERS:
                    if player_team != team:
                        continue
                    is_postponed_row = (
                        postpone_player_5001_gw is not None
                        and code == 5001
                        and gw == postpone_player_5001_gw
                    )
                    if is_postponed_row and postponed_player_actual_started is not None:
                        started = postponed_player_actual_started
                    else:
                        started = not (vary_starts and code == 5001 and gw % 2 == 0)
                    minutes = 90 if started else 0
                    row_kickoff = kickoff_dt
                    if is_postponed_row:
                        # Postponed far into the future -- still labelled GW
                        # postpone_player_5001_gw, but its outcome (kickoff +
                        # outcome_delay) will not be available until well
                        # after later gameweeks' own deadlines.
                        row_kickoff = base_kickoff + timedelta(weeks=num_gameweeks + 4)
                    rows.append(
                        {
                            "element": element, "code": code, "position": position, "team": team,
                            "GW": gw, "fixture": fixture_id, "kickoff_time": row_kickoff.isoformat(),
                            "was_home": was_home, "opponent_team": opponent,
                            "team_a_score": 1, "team_h_score": 2,
                            "minutes": minutes, "starts": 1 if started else 0,
                            "expected_goals": 0.1 if position != "GK" and started else 0.0,
                            "expected_assists": 0.05 if position != "GK" and started else 0.0,
                            "expected_goals_conceded": 1.0 if started else 0.0,
                            "goals_conceded": 1,
                            "saves": 4 if position == "GK" and started else 0,
                            "yellow_cards": 0, "red_cards": 0,
                            "bonus": 1 if started else 0, "bps": 20 if started else 0,
                            "defensive_contribution": 10 if position != "GK" and started else 0,
                            "total_points": 2 if started else 0,
                        }
                    )
    return pd.DataFrame(rows)


def _import(
    tmp_path,
    *,
    num_gameweeks: int = 8,
    vary_starts: bool = False,
    postpone_player_5001_gw: int | None = None,
    postponed_player_actual_started: bool | None = None,
):
    gameweeks_frame = _gameweeks(
        num_gameweeks=num_gameweeks,
        vary_starts=vary_starts,
        postpone_player_5001_gw=postpone_player_5001_gw,
        postponed_player_actual_started=postponed_player_actual_started,
    )
    players_frame = _players_raw(gameweeks_frame)
    players_path = tmp_path / "players_raw.csv"
    gameweeks_path = tmp_path / "merged_gw.csv"
    players_frame.to_csv(players_path, index=False)
    gameweeks_frame.to_csv(gameweeks_path, index=False)
    database_path = tmp_path / "fpl.duckdb"
    timestamp = datetime(2025, 10, 1, 12, 0, tzinfo=UTC)
    result = import_player_fixture_history(
        players_path, gameweeks_path, season="2025-26",
        source_revision="abc123", source_committed_at=timestamp,
        imported_at=timestamp, database_path=database_path,
    )
    return result, database_path, players_frame, gameweeks_frame


def test_policy_backtest_scores_all_three_policies_with_equal_row_counts(tmp_path):
    result, database_path, players_frame, gameweeks_frame = _import(tmp_path, num_gameweeks=8)

    with duckdb.connect(str(database_path)) as connection:
        bundle = materialize_appearance_policy_backtest(
            season="2025-26",
            import_run_id=result.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_frame,
            players_raw_frame=players_frame,
            evaluation_from_gw=3,
            evaluation_to_gw=8,
            minimum_calibration_gameweeks=2,
        )

    assert set(bundle.results_by_policy) == set(POLICIES)
    row_counts = {policy: len(r.observations) for policy, r in bundle.results_by_policy.items()}
    assert len(set(row_counts.values())) == 1  # every policy scores the SAME candidate rows
    assert row_counts["raw"] > 0


def test_policy_backtest_raw_and_policies_agree_before_any_fit_is_eligible(tmp_path):
    # With a high minimum_calibration_gameweeks, no fit is ever eligible over
    # this short fixture -- global/high_end_shrinkage must equal raw exactly
    # for every row (never a fabricated calibration).
    result, database_path, players_frame, gameweeks_frame = _import(tmp_path, num_gameweeks=5)

    with duckdb.connect(str(database_path)) as connection:
        bundle = materialize_appearance_policy_backtest(
            season="2025-26",
            import_run_id=result.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_frame,
            players_raw_frame=players_frame,
            evaluation_from_gw=3,
            evaluation_to_gw=5,
            minimum_calibration_gameweeks=10,  # unreachable within this fixture
        )

    raw_xpts = [o.predicted_xpts for o in bundle.results_by_policy["raw"].observations]
    global_xpts = [o.predicted_xpts for o in bundle.results_by_policy["global"].observations]
    shrink_xpts = [o.predicted_xpts for o in bundle.results_by_policy["high_end_shrinkage"].observations]

    assert raw_xpts == pytest.approx(global_xpts)
    assert raw_xpts == pytest.approx(shrink_xpts)
    assert all(fit is None for _, fit in bundle.results_by_policy["global"].fit_trajectory)


def test_policy_backtest_gaps_are_identical_across_policies(tmp_path):
    # Gap eligibility depends only on policy-independent inputs -- a row
    # excluded under one policy must be excluded under all three.
    result, database_path, players_frame, gameweeks_frame = _import(tmp_path, num_gameweeks=8)

    with duckdb.connect(str(database_path)) as connection:
        bundle = materialize_appearance_policy_backtest(
            season="2025-26",
            import_run_id=result.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_frame,
            players_raw_frame=players_frame,
            evaluation_from_gw=3,
            evaluation_to_gw=8,
            minimum_calibration_gameweeks=2,
        )

    total_scored = len(bundle.results_by_policy["raw"].observations)
    # (No DGWs or other exclusions expected in this clean fixture.)
    assert total_scored == bundle.candidate_player_fixture_rows


def test_policy_backtest_reuses_score_predictions(tmp_path):
    result, database_path, players_frame, gameweeks_frame = _import(tmp_path, num_gameweeks=8)

    with duckdb.connect(str(database_path)) as connection:
        bundle = materialize_appearance_policy_backtest(
            season="2025-26",
            import_run_id=result.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_frame,
            players_raw_frame=players_frame,
            evaluation_from_gw=3,
            evaluation_to_gw=8,
            minimum_calibration_gameweeks=2,
        )

    for policy in POLICIES:
        metrics = score_predictions(bundle.results_by_policy[policy].observations)
        assert metrics.observations == len(bundle.results_by_policy[policy].observations)


def test_policy_backtest_rejects_invalid_evaluation_range(tmp_path):
    result, database_path, players_frame, gameweeks_frame = _import(tmp_path, num_gameweeks=5)

    with duckdb.connect(str(database_path)) as connection:
        with pytest.raises(ValueError, match="evaluation_from_gw"):
            materialize_appearance_policy_backtest(
                season="2025-26",
                import_run_id=result.import_run_id,
                connection=connection,
                gameweeks_frame=gameweeks_frame,
                players_raw_frame=players_frame,
                evaluation_from_gw=5,
                evaluation_to_gw=3,
            )


def test_policy_backtest_rejects_invalid_minimum_calibration_gameweeks(tmp_path):
    result, database_path, players_frame, gameweeks_frame = _import(tmp_path, num_gameweeks=5)

    with duckdb.connect(str(database_path)) as connection:
        with pytest.raises(ValueError, match="minimum_calibration_gameweeks"):
            materialize_appearance_policy_backtest(
                season="2025-26",
                import_run_id=result.import_run_id,
                connection=connection,
                gameweeks_frame=gameweeks_frame,
                players_raw_frame=players_frame,
                minimum_calibration_gameweeks=0,
            )


def test_policy_backtest_calibration_actually_changes_predicted_xpts_once_a_fit_is_eligible(tmp_path):
    # With real appearance-outcome variance (one player alternates
    # started/absent), a walk-forward start_probability fit becomes
    # eligible and non-degenerate -- global's predicted_xpts must then
    # differ from raw's for at least the calibrated rows, proving the
    # rescaled AppearanceProjection genuinely reaches predicted_xpts (not
    # merely stored and ignored, as an independent expected_minutes
    # calibration would be).
    result, database_path, players_frame, gameweeks_frame = _import(
        tmp_path, num_gameweeks=10, vary_starts=True
    )

    with duckdb.connect(str(database_path)) as connection:
        bundle = materialize_appearance_policy_backtest(
            season="2025-26",
            import_run_id=result.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_frame,
            players_raw_frame=players_frame,
            evaluation_from_gw=6,
            evaluation_to_gw=10,
            minimum_calibration_gameweeks=3,
        )

    fits_used = [fit for _, fit in bundle.results_by_policy["global"].fit_trajectory]
    assert any(fit is not None for fit in fits_used)  # sanity: at least one eligible step

    raw_by_key = {
        (o.player_id, o.fixture_id, o.gameweek): o.predicted_xpts
        for o in bundle.results_by_policy["raw"].observations
    }
    global_by_key = {
        (o.player_id, o.fixture_id, o.gameweek): o.predicted_xpts
        for o in bundle.results_by_policy["global"].observations
    }
    assert set(raw_by_key) == set(global_by_key)  # same candidate rows

    differing = sum(
        1
        for key in raw_by_key
        if abs(raw_by_key[key] - global_by_key[key]) > 1e-9
    )
    assert differing > 0  # calibration must have moved at least one row's score


def test_policy_backtest_high_end_shrinkage_scores_the_same_candidate_rows_as_raw(tmp_path):
    # End-to-end structural guarantee (the precise threshold boundary
    # behaviour itself is covered by the unit-level apply_calibration_policy
    # tests above, which can engineer exact above/below-threshold
    # AppearanceProjection values directly).
    result, database_path, players_frame, gameweeks_frame = _import(
        tmp_path, num_gameweeks=10, vary_starts=True
    )

    with duckdb.connect(str(database_path)) as connection:
        bundle = materialize_appearance_policy_backtest(
            season="2025-26",
            import_run_id=result.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_frame,
            players_raw_frame=players_frame,
            evaluation_from_gw=6,
            evaluation_to_gw=10,
            minimum_calibration_gameweeks=3,
        )

    raw_keys = {
        (o.player_id, o.fixture_id, o.gameweek)
        for o in bundle.results_by_policy["raw"].observations
    }
    shrink_keys = {
        (o.player_id, o.fixture_id, o.gameweek)
        for o in bundle.results_by_policy["high_end_shrinkage"].observations
    }
    assert raw_keys == shrink_keys


# ---------------------------------------------------------------------------
# End-to-end postponed-fixture regression
# ---------------------------------------------------------------------------


def test_policy_backtest_postponed_fixture_outcome_is_excluded_from_a_fit_it_could_not_have_informed(tmp_path):
    # Player 5001's GW3 fixture is "postponed" (kicks off far later than its
    # GW3 label suggests) -- its outcome must not be available in time to
    # enter the calibration fit used for gameweek 6's predictions.
    #
    # This is NOT a "predictions stay identical" test: correctly excluding a
    # row from the training set legitimately shifts the OLS fit (and so
    # every row's calibrated prediction) versus a run where that row was
    # never postponed at all -- that is the fix working, not a bug. What
    # must hold is the STRUCTURAL guarantee: the postponed run's GW6 fit is
    # trained on strictly FEWER rows than the baseline run's GW6 fit (player
    # 5001's GW3 row is missing), proving the postponed row was actually
    # excluded rather than silently retained under its earlier gameweek
    # label -- which is exactly what the old `gameweek < target` check
    # alone (without the outcome_available_at filter) would have gotten
    # wrong, since GW3 < GW6 regardless of the fixture's real kickoff.
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir()
    baseline_result, baseline_db, baseline_players, baseline_gameweeks = _import(
        baseline_dir, num_gameweeks=10, vary_starts=True
    )
    with duckdb.connect(str(baseline_db)) as connection:
        baseline_bundle = materialize_appearance_policy_backtest(
            season="2025-26",
            import_run_id=baseline_result.import_run_id,
            connection=connection,
            gameweeks_frame=baseline_gameweeks,
            players_raw_frame=baseline_players,
            evaluation_from_gw=1,
            evaluation_to_gw=6,
            minimum_calibration_gameweeks=3,
        )

    postponed_dir = tmp_path / "postponed"
    postponed_dir.mkdir()
    postponed_result, postponed_db, postponed_players, postponed_gameweeks = _import(
        postponed_dir, num_gameweeks=10, vary_starts=True, postpone_player_5001_gw=3
    )
    with duckdb.connect(str(postponed_db)) as connection:
        postponed_bundle = materialize_appearance_policy_backtest(
            season="2025-26",
            import_run_id=postponed_result.import_run_id,
            connection=connection,
            gameweeks_frame=postponed_gameweeks,
            players_raw_frame=postponed_players,
            evaluation_from_gw=1,
            evaluation_to_gw=6,
            minimum_calibration_gameweeks=3,
        )

    baseline_fit = dict(baseline_bundle.results_by_policy["global"].fit_trajectory)[6]
    postponed_fit = dict(postponed_bundle.results_by_policy["global"].fit_trajectory)[6]
    assert baseline_fit is not None
    assert postponed_fit is not None
    # Player 5001's GW3 row (1 row, since only one fixture in GW3 involves
    # player 5001) is present in baseline's training set but excluded from
    # postponed's -- exactly one fewer training row.
    assert postponed_fit.training_rows == baseline_fit.training_rows - 1
    # And the two fits must therefore genuinely differ (proving the missing
    # row actually changed the fit, not merely that the count differs while
    # the fit coincidentally stayed the same).
    assert (postponed_fit.slope, postponed_fit.intercept) != (baseline_fit.slope, baseline_fit.intercept)


def test_policy_backtest_postponed_fixtures_own_row_is_absent_from_the_fits_training_gameweek(tmp_path):
    # Complementary check: directly confirm the postponed row's own
    # (player, target) contribution is what's missing, by reconstructing
    # what an UNFIXED (gameweek-label-only) filter would have included and
    # showing the fixed fit's training_rows is smaller than that count.
    postponed_dir = tmp_path / "postponed"
    postponed_dir.mkdir()
    postponed_result, postponed_db, postponed_players, postponed_gameweeks = _import(
        postponed_dir, num_gameweeks=10, vary_starts=True, postpone_player_5001_gw=3
    )
    with duckdb.connect(str(postponed_db)) as connection:
        postponed_bundle = materialize_appearance_policy_backtest(
            season="2025-26",
            import_run_id=postponed_result.import_run_id,
            connection=connection,
            gameweeks_frame=postponed_gameweeks,
            players_raw_frame=postponed_players,
            evaluation_from_gw=3,
            evaluation_to_gw=6,
            minimum_calibration_gameweeks=1,
        )

    fit_trajectory = dict(postponed_bundle.results_by_policy["global"].fit_trajectory)
    # With evaluation_from_gw=3, calibration_rows only accumulate starting
    # at GW3 -- GW6's fit is trained on GW3+GW4+GW5 (3 prior gameweeks x 5
    # players = 15 rows), MINUS the postponed player's excluded GW3 row =
    # 14; would be 15 if the postponed row had leaked in. Restated over a
    # different evaluation window than the primary test above, to guard
    # against this being an artifact of that test's specific
    # evaluation_from_gw/to_gw.
    assert fit_trajectory[6] is not None
    gw6_training_rows = fit_trajectory[6].training_rows
    assert gw6_training_rows == 3 * len(PLAYERS) - 1


def test_policy_backtest_full_pipeline_mutation_invariance_for_an_unavailable_postponed_outcome(tmp_path):
    # The required end-to-end mutation test: two otherwise BYTE-IDENTICAL
    # synthetic runs, differing ONLY in the postponed row's realised
    # actual_started outcome (True in one, False in the other). Both runs
    # share: the same earlier gameweek label (GW3) for the postponed
    # fixture, and the same postponed kickoff/outcome_available_at (falls
    # after GW6's own deadline). For a target fold (GW6) strictly BEFORE
    # that outcome becomes available, the two runs must produce IDENTICAL
    # results for every OTHER row: the fitted OLS record used at GW6, and
    # every global/high_end_shrinkage/raw predicted_xpts by key. If the
    # postponed outcome had leaked into GW6's fit, the two runs would
    # disagree (their only difference is that one boolean), so this test
    # fails if the outcome_available_at condition is removed from
    # fit_for_gameweek.
    started_true_dir = tmp_path / "started_true"
    started_true_dir.mkdir()
    started_true_result, started_true_db, started_true_players, started_true_gameweeks = _import(
        started_true_dir,
        num_gameweeks=10,
        vary_starts=True,
        postpone_player_5001_gw=3,
        postponed_player_actual_started=True,
    )
    with duckdb.connect(str(started_true_db)) as connection:
        started_true_bundle = materialize_appearance_policy_backtest(
            season="2025-26",
            import_run_id=started_true_result.import_run_id,
            connection=connection,
            gameweeks_frame=started_true_gameweeks,
            players_raw_frame=started_true_players,
            evaluation_from_gw=1,
            evaluation_to_gw=6,
            minimum_calibration_gameweeks=3,
        )

    started_false_dir = tmp_path / "started_false"
    started_false_dir.mkdir()
    started_false_result, started_false_db, started_false_players, started_false_gameweeks = _import(
        started_false_dir,
        num_gameweeks=10,
        vary_starts=True,
        postpone_player_5001_gw=3,
        postponed_player_actual_started=False,
    )
    with duckdb.connect(str(started_false_db)) as connection:
        started_false_bundle = materialize_appearance_policy_backtest(
            season="2025-26",
            import_run_id=started_false_result.import_run_id,
            connection=connection,
            gameweeks_frame=started_false_gameweeks,
            players_raw_frame=started_false_players,
            evaluation_from_gw=1,
            evaluation_to_gw=6,
            minimum_calibration_gameweeks=3,
        )

    # 1. The fitted OLS record used at GW6 must be IDENTICAL -- the two
    # runs' only difference (the postponed row's actual_started) must not
    # be visible to a fit whose deadline precedes that outcome's own
    # availability.
    started_true_fit = dict(started_true_bundle.results_by_policy["global"].fit_trajectory)[6]
    started_false_fit = dict(started_false_bundle.results_by_policy["global"].fit_trajectory)[6]
    assert started_true_fit is not None
    assert started_false_fit is not None
    assert started_true_fit == started_false_fit

    # 2. global/high_end_shrinkage predicted_xpts must be IDENTICAL by key
    # for every row scored at GW6 (the postponed player's own GW3 row is a
    # different fixture/gameweek and is deliberately excluded from this
    # comparison -- ITS OWN score legitimately differs between the two
    # runs, since its own actual_started outcome differs; what must NOT
    # differ is every OTHER row's calibrated prediction).
    for policy in ("global", "high_end_shrinkage", "raw"):
        started_true_by_key = {
            (o.player_id, o.fixture_id, o.gameweek): o.predicted_xpts
            for o in started_true_bundle.results_by_policy[policy].observations
            if o.gameweek == 6
        }
        started_false_by_key = {
            (o.player_id, o.fixture_id, o.gameweek): o.predicted_xpts
            for o in started_false_bundle.results_by_policy[policy].observations
            if o.gameweek == 6
        }
        assert set(started_true_by_key) == set(started_false_by_key)
        assert started_true_by_key == pytest.approx(started_false_by_key), (
            f"{policy} GW6 predictions differ between the two mutation runs -- the postponed "
            "row's actual_started outcome leaked into a fold before it was available"
        )


# ---------------------------------------------------------------------------
# verify_raw_row_level_parity
# ---------------------------------------------------------------------------


def test_raw_row_level_parity_matches_the_canonical_materializer(tmp_path):
    result, database_path, players_frame, gameweeks_frame = _import(tmp_path, num_gameweeks=8)

    with duckdb.connect(str(database_path)) as connection:
        bundle = materialize_appearance_policy_backtest(
            season="2025-26",
            import_run_id=result.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_frame,
            players_raw_frame=players_frame,
            evaluation_from_gw=3,
            evaluation_to_gw=8,
            minimum_calibration_gameweeks=2,
        )
        canonical = materialize_benchwarmers_walk_forward_backtest(
            season="2025-26",
            import_run_id=result.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_frame,
            players_raw_frame=players_frame,
            evaluation_from_gw=3,
            evaluation_to_gw=8,
        )

    parity = verify_raw_row_level_parity(
        canonical,
        bundle.results_by_policy["raw"].observations,
        bundle.gaps,
        candidate_player_fixture_rows=bundle.candidate_player_fixture_rows,
        evaluated_gameweeks=bundle.evaluated_gameweeks,
    )

    assert parity.matches is True
    assert parity.mismatches == ()
    assert parity.canonical_rows == parity.policy_backtest_rows
    assert parity.canonical_rows > 0


def test_raw_row_level_parity_detects_a_predicted_xpts_mismatch(tmp_path):
    result, database_path, players_frame, gameweeks_frame = _import(tmp_path, num_gameweeks=8)

    with duckdb.connect(str(database_path)) as connection:
        bundle = materialize_appearance_policy_backtest(
            season="2025-26",
            import_run_id=result.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_frame,
            players_raw_frame=players_frame,
            evaluation_from_gw=3,
            evaluation_to_gw=8,
            minimum_calibration_gameweeks=2,
        )
        canonical = materialize_benchwarmers_walk_forward_backtest(
            season="2025-26",
            import_run_id=result.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_frame,
            players_raw_frame=players_frame,
            evaluation_from_gw=3,
            evaluation_to_gw=8,
        )

    raw_observations = list(bundle.results_by_policy["raw"].observations)
    tampered_first = raw_observations[0]
    raw_observations[0] = replace(tampered_first, predicted_xpts=tampered_first.predicted_xpts + 1.0)

    parity = verify_raw_row_level_parity(
        canonical,
        tuple(raw_observations),
        bundle.gaps,
        candidate_player_fixture_rows=bundle.candidate_player_fixture_rows,
        evaluated_gameweeks=bundle.evaluated_gameweeks,
    )

    assert parity.matches is False
    assert any("predicted_xpts" in m for m in parity.mismatches)


def test_raw_row_level_parity_detects_a_missing_row():
    deadline = datetime(2025, 8, 16, 12, 30, tzinfo=UTC)
    kickoff = deadline + timedelta(minutes=90)
    obs = BacktestObservation(
        season="2025-26", gameweek=3, deadline=deadline, fixture_kickoff=kickoff,
        feature_cutoff=deadline, outcome_available_at=kickoff + timedelta(hours=3),
        player_id=1, fixture_id=100, predicted_xpts=2.0, actual_points=2.0,
    )
    canonical = BenchwarmersBacktestResult(
        observations=(obs,), diagnostics=(), appearance_observations=(),
        gaps=(), evaluated_gameweeks=(3,), candidate_player_fixture_rows=1,
        dgw_excluded_appearance_rows=0, missing_appearance_rows=0, scored_player_fixture_rows=1,
    )

    parity = verify_raw_row_level_parity(
        canonical, (), (), candidate_player_fixture_rows=1, evaluated_gameweeks=(3,)
    )

    assert parity.matches is False
    assert any("missing from the policy backtest" in m for m in parity.mismatches)


def test_raw_row_level_parity_detects_a_gap_flag_mismatch():
    canonical_gap = BacktestGapSummary(3, 100, 1, "A", "DEF", ("MISSING_TEAM_STRENGTH",))
    canonical = BenchwarmersBacktestResult(
        observations=(), diagnostics=(), appearance_observations=(),
        gaps=(canonical_gap,), evaluated_gameweeks=(3,), candidate_player_fixture_rows=1,
        dgw_excluded_appearance_rows=0, missing_appearance_rows=0, scored_player_fixture_rows=0,
    )
    policy_backtest_gap = AppearancePolicyBacktestGap(3, 100, 1, "A", "DEF", ("MISSING_TEAM_ID",))

    parity = verify_raw_row_level_parity(
        canonical, (), (policy_backtest_gap,),
        candidate_player_fixture_rows=1, evaluated_gameweeks=(3,),
    )

    assert parity.matches is False
    assert any("gap" in m and "flags" in m for m in parity.mismatches)


def test_raw_row_level_parity_detects_a_season_mismatch():
    deadline = datetime(2025, 8, 16, 12, 30, tzinfo=UTC)
    kickoff = deadline + timedelta(minutes=90)
    canonical_obs = BacktestObservation(
        season="2025-26", gameweek=3, deadline=deadline, fixture_kickoff=kickoff,
        feature_cutoff=deadline, outcome_available_at=kickoff + timedelta(hours=3),
        player_id=1, fixture_id=100, predicted_xpts=2.0, actual_points=2.0,
    )
    # Same key, same every other field, but a DIFFERENT season string --
    # must be caught even though the two materializers otherwise agree
    # exactly (season is compared like any other field, not exempted as
    # caller-supplied).
    policy_obs = replace(canonical_obs, season="2024-25")
    canonical = BenchwarmersBacktestResult(
        observations=(canonical_obs,), diagnostics=(), appearance_observations=(),
        gaps=(), evaluated_gameweeks=(3,), candidate_player_fixture_rows=1,
        dgw_excluded_appearance_rows=0, missing_appearance_rows=0, scored_player_fixture_rows=1,
    )

    parity = verify_raw_row_level_parity(
        canonical, (policy_obs,), (), candidate_player_fixture_rows=1, evaluated_gameweeks=(3,)
    )

    assert parity.matches is False
    assert any(".season" in m for m in parity.mismatches)


def test_raw_row_level_parity_detects_a_gap_team_mismatch():
    canonical_gap = BacktestGapSummary(3, 100, 1, "A", "DEF", ("MISSING_TEAM_STRENGTH",))
    # Same key (player_code, fixture_id, gameweek) and same flags, but a
    # DIFFERENT team -- must be caught even though flags alone would agree
    # (team/position are compared as their own fields, not folded into the
    # flags-only comparison the earlier implementation used).
    policy_backtest_gap = AppearancePolicyBacktestGap(3, 100, 1, "B", "DEF", ("MISSING_TEAM_STRENGTH",))
    canonical = BenchwarmersBacktestResult(
        observations=(), diagnostics=(), appearance_observations=(),
        gaps=(canonical_gap,), evaluated_gameweeks=(3,), candidate_player_fixture_rows=1,
        dgw_excluded_appearance_rows=0, missing_appearance_rows=0, scored_player_fixture_rows=0,
    )

    parity = verify_raw_row_level_parity(
        canonical, (), (policy_backtest_gap,),
        candidate_player_fixture_rows=1, evaluated_gameweeks=(3,),
    )

    assert parity.matches is False
    assert any("gap" in m and ".team" in m for m in parity.mismatches)


def test_raw_row_level_parity_detects_a_gap_position_mismatch():
    canonical_gap = BacktestGapSummary(3, 100, 1, "A", "DEF", ("MISSING_TEAM_STRENGTH",))
    # Same key and same flags, but a DIFFERENT position -- must be caught.
    policy_backtest_gap = AppearancePolicyBacktestGap(3, 100, 1, "A", "MID", ("MISSING_TEAM_STRENGTH",))
    canonical = BenchwarmersBacktestResult(
        observations=(), diagnostics=(), appearance_observations=(),
        gaps=(canonical_gap,), evaluated_gameweeks=(3,), candidate_player_fixture_rows=1,
        dgw_excluded_appearance_rows=0, missing_appearance_rows=0, scored_player_fixture_rows=0,
    )

    parity = verify_raw_row_level_parity(
        canonical, (), (policy_backtest_gap,),
        candidate_player_fixture_rows=1, evaluated_gameweeks=(3,),
    )

    assert parity.matches is False
    assert any("gap" in m and ".position" in m for m in parity.mismatches)


def test_raw_row_level_parity_detects_a_duplicate_observation_key():
    deadline = datetime(2025, 8, 16, 12, 30, tzinfo=UTC)
    kickoff = deadline + timedelta(minutes=90)
    obs = BacktestObservation(
        season="2025-26", gameweek=3, deadline=deadline, fixture_kickoff=kickoff,
        feature_cutoff=deadline, outcome_available_at=kickoff + timedelta(hours=3),
        player_id=1, fixture_id=100, predicted_xpts=2.0, actual_points=2.0,
    )
    # TWO rows sharing the exact same (player_id, fixture_id, gameweek) key
    # on the policy-backtest side -- a naive dict comprehension would
    # silently keep only the second and report a spurious match against a
    # canonical side with just one row at that key. Give the duplicate a
    # DIFFERENT predicted_xpts so a dict-based comparison using only the
    # last-built dict would otherwise still "pass" undetected.
    duplicate_obs = replace(obs, predicted_xpts=99.0)
    canonical = BenchwarmersBacktestResult(
        observations=(obs,), diagnostics=(), appearance_observations=(),
        gaps=(), evaluated_gameweeks=(3,), candidate_player_fixture_rows=1,
        dgw_excluded_appearance_rows=0, missing_appearance_rows=0, scored_player_fixture_rows=1,
    )

    parity = verify_raw_row_level_parity(
        canonical, (obs, duplicate_obs), (), candidate_player_fixture_rows=1, evaluated_gameweeks=(3,)
    )

    assert parity.matches is False
    assert any("duplicate observation key" in m for m in parity.mismatches)


def test_raw_row_level_parity_detects_a_duplicate_gap_key():
    # TWO gaps sharing the exact same (player_code, fixture_id, gameweek)
    # key on the canonical side -- must be flagged, not silently collapsed
    # by dict construction.
    gap_a = BacktestGapSummary(3, 100, 1, "A", "DEF", ("MISSING_TEAM_STRENGTH",))
    gap_b = BacktestGapSummary(3, 100, 1, "A", "DEF", ("MISSING_TEAM_ID",))
    canonical = BenchwarmersBacktestResult(
        observations=(), diagnostics=(), appearance_observations=(),
        gaps=(gap_a, gap_b), evaluated_gameweeks=(3,), candidate_player_fixture_rows=1,
        dgw_excluded_appearance_rows=0, missing_appearance_rows=0, scored_player_fixture_rows=0,
    )
    policy_backtest_gap = AppearancePolicyBacktestGap(3, 100, 1, "A", "DEF", ("MISSING_TEAM_STRENGTH",))

    parity = verify_raw_row_level_parity(
        canonical, (), (policy_backtest_gap,),
        candidate_player_fixture_rows=1, evaluated_gameweeks=(3,),
    )

    assert parity.matches is False
    assert any("duplicate gap key" in m for m in parity.mismatches)
