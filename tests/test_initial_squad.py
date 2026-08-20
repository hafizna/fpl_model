from __future__ import annotations

from collections import Counter

import pytest

from fpl_model.decision.initial_squad import optimize_initial_squad
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

