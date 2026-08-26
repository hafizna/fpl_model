from __future__ import annotations

from collections import Counter

import pytest

from fpl_model.decision.initial_squad import (
    SquadConstraints,
    optimize_initial_squad,
)
from fpl_model.decision.lineup import PlayerGameweekProjection
from fpl_model.decision.rolling import GameweekProjectionPool
from fpl_model.decision.squad import SquadPlayer
from fpl_model.decision.transfer import TransferTarget


def _target(
    fpl_id: int,
    position: str,
    *,
    price: int = 50,
    xpts: float = 3.0,
    team_id: int | None = None,
) -> TransferTarget:
    player = SquadPlayer(
        fpl_id=fpl_id,
        player_code=10_000 + fpl_id,
        player_name=f"Player {fpl_id}",
        team_id=team_id or ((fpl_id - 1) % 8) + 1,
        position=position,
        current_price_tenths=price,
        purchase_price_tenths=price,
        selling_price_tenths=price,
        squad_position=1,
        is_captain=False,
        is_vice_captain=False,
    )
    return TransferTarget(
        player=player,
        projection=PlayerGameweekProjection(fpl_id=fpl_id, expected_points=xpts),
    )


def _players() -> tuple[TransferTarget, ...]:
    rows = [
        *(_target(fpl_id, "GK", xpts=float(fpl_id)) for fpl_id in range(1, 3)),
        *(_target(fpl_id, "DEF", xpts=float(fpl_id)) for fpl_id in range(3, 8)),
        *(_target(fpl_id, "MID", xpts=float(fpl_id)) for fpl_id in range(8, 13)),
        *(_target(fpl_id, "FWD", xpts=float(fpl_id)) for fpl_id in range(13, 16)),
    ]
    rows.extend(
        (
            _target(16, "FWD", xpts=30.0),
            _target(17, "MID", xpts=25.0),
            _target(18, "DEF", xpts=20.0),
            _target(19, "GK", price=1_000, xpts=100.0),
        )
    )
    return tuple(rows)


def _pools() -> tuple[GameweekProjectionPool, ...]:
    rows = _players()
    return tuple(
        GameweekProjectionPool(
            gameweek=gameweek,
            players=rows,
            transferable_fpl_ids=tuple(row.player.fpl_id for row in rows),
        )
        for gameweek in (1, 2, 3)
    )


def test_builds_a_legal_budgeted_squad_and_rescores_each_gameweek():
    result = optimize_initial_squad(
        _pools(),
        beam_width=500,
        candidates_per_position_per_lens=20,
    )
    plan = result.recommended

    assert len(plan.squad.players) == 15
    assert Counter(player.position for player in plan.squad.players) == {
        "GK": 2,
        "DEF": 5,
        "MID": 5,
        "FWD": 3,
    }
    assert max(Counter(player.team_id for player in plan.squad.players).values()) <= 3
    assert plan.squad_cost_tenths + plan.bank_tenths == 1_000
    assert plan.squad.unlimited_transfers is True
    assert [row.gameweek for row in plan.gameweeks] == [1, 2, 3]
    assert all(len(row.lineup.starters) == 11 for row in plan.gameweeks)
    assert 16 in {player.fpl_id for player in plan.squad.players}
    assert 19 not in {player.fpl_id for player in plan.squad.players}
    assert result.search_is_exact is False


def test_enforces_three_player_club_limit_even_for_high_scoring_players():
    rows = list(_players())
    for index in range(4):
        original = rows[index]
        rows[index] = _target(
            original.player.fpl_id,
            original.player.position,
            xpts=50.0,
            team_id=20,
        )
    pools = tuple(
        GameweekProjectionPool(gameweek=gameweek, players=tuple(rows))
        for gameweek in (1, 2, 3)
    )

    result = optimize_initial_squad(
        pools,
        beam_width=500,
        candidates_per_position_per_lens=20,
    )

    assert sum(player.team_id == 20 for player in result.recommended.squad.players) == 3


def test_requires_consecutive_pools_and_complete_cross_horizon_player_coverage():
    pools = _pools()
    with pytest.raises(ValueError, match="must be consecutive"):
        optimize_initial_squad((pools[0], pools[2], pools[1]))

    missing_high_forward = GameweekProjectionPool(
        gameweek=2,
        players=tuple(row for row in pools[1].players if row.player.fpl_id != 16),
    )
    result = optimize_initial_squad(
        (pools[0], missing_high_forward, pools[2]),
        beam_width=500,
        candidates_per_position_per_lens=20,
    )
    assert 16 not in result.eligible_player_ids
    assert 16 not in {player.fpl_id for player in result.recommended.squad.players}


def test_search_is_deterministic_for_identical_inputs():
    first = optimize_initial_squad(
        _pools(), beam_width=500, candidates_per_position_per_lens=20
    )
    second = optimize_initial_squad(
        _pools(), beam_width=500, candidates_per_position_per_lens=20
    )

    assert first == second


def test_locked_player_is_forced_into_the_squad_despite_being_pruned_on_price():
    # Player 19 (a GBP 100.0m goalkeeper) is priced so high the unconstrained
    # search never picks it -- it would consume the entire budget alone.
    unconstrained = optimize_initial_squad(
        _pools(), beam_width=500, candidates_per_position_per_lens=20
    )
    assert 19 not in {player.fpl_id for player in unconstrained.recommended.squad.players}

    locked = optimize_initial_squad(
        _pools(),
        beam_width=500,
        candidates_per_position_per_lens=20,
        budget_tenths=2_000,
        constraints=SquadConstraints(locked_fpl_ids=frozenset({19})),
    )

    squad_ids = {player.fpl_id for player in locked.recommended.squad.players}
    assert 19 in squad_ids
    assert len(locked.recommended.squad.players) == 15


def test_excluded_player_never_appears_in_any_returned_squad():
    result = optimize_initial_squad(
        _pools(),
        beam_width=500,
        candidates_per_position_per_lens=20,
        constraints=SquadConstraints(excluded_fpl_ids=frozenset({16})),
    )

    assert 16 not in {player.fpl_id for player in result.recommended.squad.players}
    for plan in result.alternatives:
        assert 16 not in {player.fpl_id for player in plan.squad.players}
    assert 16 not in result.candidate_player_ids


def test_locking_multiple_players_across_positions_keeps_them_all():
    # 16=FWD, 17=MID, 18=DEF -- one lock per distinct position.
    result = optimize_initial_squad(
        _pools(),
        beam_width=500,
        candidates_per_position_per_lens=20,
        constraints=SquadConstraints(locked_fpl_ids=frozenset({16, 17, 18})),
    )

    squad_ids = {player.fpl_id for player in result.recommended.squad.players}
    assert {16, 17, 18}.issubset(squad_ids)
    assert len(result.recommended.squad.players) == 15


def test_rejects_a_player_both_locked_and_excluded():
    with pytest.raises(ValueError, match="cannot be both locked and excluded"):
        SquadConstraints(locked_fpl_ids=frozenset({16}), excluded_fpl_ids=frozenset({16}))


def test_rejects_locking_a_player_missing_from_the_full_horizon():
    pools = _pools()
    missing_high_forward = GameweekProjectionPool(
        gameweek=2,
        players=tuple(row for row in pools[1].players if row.player.fpl_id != 16),
    )

    with pytest.raises(ValueError, match="lack a complete, transferable"):
        optimize_initial_squad(
            (pools[0], missing_high_forward, pools[2]),
            beam_width=500,
            candidates_per_position_per_lens=20,
            constraints=SquadConstraints(locked_fpl_ids=frozenset({16})),
        )


def test_rejects_locking_too_many_players_in_one_position():
    # GK requires exactly 2; locking three GKs can never be legal.
    with pytest.raises(ValueError, match="exceed the legal position counts"):
        optimize_initial_squad(
            _pools(),
            beam_width=500,
            candidates_per_position_per_lens=20,
            constraints=SquadConstraints(locked_fpl_ids=frozenset({1, 2, 19})),
        )


def test_rejects_locking_more_than_three_players_from_one_club():
    rows = list(_players())
    for index in range(4):
        original = rows[index]
        rows[index] = _target(original.player.fpl_id, original.player.position, team_id=20)
    pools = tuple(
        GameweekProjectionPool(gameweek=gameweek, players=tuple(rows))
        for gameweek in (1, 2, 3)
    )
    locked_ids = frozenset(row.player.fpl_id for row in rows[:4])

    with pytest.raises(ValueError, match="exceed the three-player club limit"):
        optimize_initial_squad(
            pools,
            beam_width=500,
            candidates_per_position_per_lens=20,
            constraints=SquadConstraints(locked_fpl_ids=locked_ids),
        )


def test_rejects_locked_players_alone_exceeding_budget():
    # Two GBP 100.0m goalkeepers alone already exceed the default GBP 100.0m budget.
    rows = list(_players())
    rows.append(_target(20, "GK", price=1_000, xpts=100.0, team_id=90))
    pools = tuple(
        GameweekProjectionPool(gameweek=gameweek, players=tuple(rows))
        for gameweek in (1, 2, 3)
    )

    with pytest.raises(ValueError, match="exceeding the"):
        optimize_initial_squad(
            pools,
            beam_width=500,
            candidates_per_position_per_lens=20,
            constraints=SquadConstraints(locked_fpl_ids=frozenset({19, 20})),
        )


def test_planned_transfers_can_choose_a_better_initial_core_then_switch():
    def rows(gameweek: int) -> tuple[TransferTarget, ...]:
        values: list[TransferTarget] = []
        fpl_id = 1
        for position, count in (("GK", 2), ("DEF", 5), ("FWD", 3)):
            for _ in range(count):
                values.append(
                    _target(fpl_id, position, xpts=2.0, team_id=100 + fpl_id)
                )
                fpl_id += 1
        for offset in range(4):
            mid_id = 20 + offset
            values.append(
                _target(mid_id, "MID", xpts=12.0, team_id=100 + mid_id)
            )
        initial_star_id = 30
        future_star_id = 31
        values.append(
            _target(
                initial_star_id,
                "MID",
                xpts=20.0 if gameweek == 1 else 0.0,
                team_id=130,
            )
        )
        values.append(
            _target(
                future_star_id,
                "MID",
                xpts=0.0 if gameweek == 1 else 15.0,
                team_id=131,
            )
        )
        return tuple(values)

    pools = tuple(
        GameweekProjectionPool(gameweek=gameweek, players=rows(gameweek))
        for gameweek in (1, 2, 3)
    )
    frozen = optimize_initial_squad(
        pools,
        beam_width=100,
        candidates_per_position_per_lens=20,
        returned_squads=1,
    )
    planned = optimize_initial_squad(
        pools,
        beam_width=100,
        candidates_per_position_per_lens=20,
        returned_squads=1,
        plan_future_transfers=True,
        planned_transfer_shortlist=10,
        transfer_beam_width=10,
        transfer_candidates_per_position=3,
    )

    frozen_ids = {player.fpl_id for player in frozen.recommended.squad.players}
    planned_ids = {player.fpl_id for player in planned.recommended.squad.players}
    assert 31 in frozen_ids and 30 not in frozen_ids
    assert 30 in planned_ids and 31 not in planned_ids
    assert planned.recommended.gameweeks[1].outgoing_fpl_id == 30
    assert planned.recommended.gameweeks[1].incoming_fpl_id == 31
    assert planned.recommended.total_transfer_cost == 0.0
    assert planned.recommended.cumulative_xpts > frozen.recommended.cumulative_xpts

