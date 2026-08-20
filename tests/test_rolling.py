from __future__ import annotations

from dataclasses import replace

import pytest

from fpl_model.decision.rolling import GameweekProjectionPool, plan_three_gameweeks
from fpl_model.decision.transfer import TransferTarget
from tests.test_lineup import _projections
from tests.test_squad import _players, _validate
from tests.test_transfer import _target


def _pool(gameweek: int, *, candidate_xpts: float | None) -> GameweekProjectionPool:
    players = _players()
    projection_by_id = {row.fpl_id: row for row in _projections()}
    targets = [
        TransferTarget(player=player, projection=projection_by_id[player.fpl_id])
        for player in players
    ]
    if candidate_xpts is not None:
        candidate = _target(xpts=candidate_xpts)
        targets.append(candidate)
    return GameweekProjectionPool(gameweek=gameweek, players=tuple(targets))


def test_waits_for_fixture_swing_and_rolls_free_transfers_before_moving():
    squad = _validate(_players(), free_transfers=1, unlimited_transfers=False)
    result = plan_three_gameweeks(
        squad,
        (
            _pool(4, candidate_xpts=0.0),
            _pool(5, candidate_xpts=0.0),
            _pool(6, candidate_xpts=12.0),
        ),
    )

    assert [step.decision for step in result.recommended.steps] == [
        "roll",
        "roll",
        "transfer",
    ]
    assert result.recommended.steps[-1].outgoing_fpl_id == 11
    assert result.recommended.steps[-1].incoming_fpl_id == 16
    assert [step.free_transfers_after for step in result.recommended.steps] == [2, 3, 3]
    assert result.recommended.total_transfer_cost == 0.0


def test_no_transfer_path_caps_rolled_free_transfers_at_five():
    result = plan_three_gameweeks(
        _validate(_players(), free_transfers=5, unlimited_transfers=False),
        (_pool(7, candidate_xpts=None), _pool(8, candidate_xpts=None), _pool(9, candidate_xpts=None)),
    )

    assert all(step.decision == "roll" for step in result.recommended.steps)
    assert result.recommended.terminal_free_transfers == 5


def test_four_point_hit_can_make_an_immediate_one_week_gain_not_worthwhile():
    result = plan_three_gameweeks(
        _validate(_players(), free_transfers=0, unlimited_transfers=False),
        (
            _pool(10, candidate_xpts=8.0),
            _pool(11, candidate_xpts=0.0),
            _pool(12, candidate_xpts=0.0),
        ),
    )

    assert result.recommended.steps[0].decision == "roll"
    assert result.recommended.total_transfer_cost == 0.0


def test_requires_complete_consecutive_three_gameweek_projection_coverage():
    incomplete = _pool(5, candidate_xpts=5.0)
    incomplete = replace(
        incomplete,
        players=tuple(row for row in incomplete.players if row.player.fpl_id != 7),
    )
    with pytest.raises(ValueError, match="GW5 is missing squad projections.*7"):
        plan_three_gameweeks(
            _validate(_players(), free_transfers=1, unlimited_transfers=False),
            (_pool(4, candidate_xpts=5.0), incomplete, _pool(6, candidate_xpts=5.0)),
        )

    with pytest.raises(ValueError, match="must be consecutive"):
        plan_three_gameweeks(
            _validate(_players(), free_transfers=1, unlimited_transfers=False),
            (_pool(4, candidate_xpts=5.0), _pool(6, candidate_xpts=5.0), _pool(7, candidate_xpts=5.0)),
        )


def test_rejects_active_chip_or_unlimited_transfer_state_instead_of_guessing_rules():
    pools = (_pool(4, candidate_xpts=5.0), _pool(5, candidate_xpts=5.0), _pool(6, candidate_xpts=5.0))
    with pytest.raises(ValueError, match="Wildcard or Free Hit"):
        plan_three_gameweeks(
            _validate(_players(), free_transfers=None, unlimited_transfers=True), pools
        )

    chip_states = {
        "wildcard": "available",
        "free_hit": "available",
        "bench_boost": "available",
        "triple_captain": "active",
    }
    with pytest.raises(ValueError, match="active chip"):
        plan_three_gameweeks(
            _validate(
                _players(),
                free_transfers=1,
                unlimited_transfers=False,
                chip_states=chip_states,
            ),
            pools,
        )


def test_search_is_deterministic_and_explicitly_approximate():
    arguments = (
        _validate(_players(), free_transfers=1, unlimited_transfers=False),
        (_pool(4, candidate_xpts=7.0), _pool(5, candidate_xpts=8.0), _pool(6, candidate_xpts=9.0)),
    )
    first = plan_three_gameweeks(*arguments, beam_width=10, candidates_per_position=3)
    second = plan_three_gameweeks(*arguments, beam_width=10, candidates_per_position=3)

    assert first == second
    assert first.search_is_exact is False
    assert first.beam_width == 10
    assert first.candidates_per_position == 3
    assert len(first.eligible_player_ids) == 16


def test_owned_untransferable_player_cannot_be_sold_then_bought_back():
    pools = (
        _pool(4, candidate_xpts=100.0),
        _pool(5, candidate_xpts=0.0),
        _pool(6, candidate_xpts=0.0),
    )
    transferable = tuple(fpl_id for fpl_id in range(1, 17) if fpl_id != 11)
    pools = tuple(replace(pool, transferable_fpl_ids=transferable) for pool in pools)
    result = plan_three_gameweeks(
        _validate(_players(), free_transfers=1, unlimited_transfers=False),
        pools,
    )

    assert result.recommended.steps[0].incoming_fpl_id == 16
    assert all(step.incoming_fpl_id != 11 for step in result.recommended.steps[1:])
