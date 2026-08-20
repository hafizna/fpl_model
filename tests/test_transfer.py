from __future__ import annotations

from dataclasses import replace

import pytest

from fpl_model.decision.lineup import PlayerGameweekProjection
from fpl_model.decision.squad import SquadPlayer
from fpl_model.decision.transfer import (
    TransferTarget,
    recommend_single_transfers,
)
from tests.test_lineup import _projections
from tests.test_squad import _players, _validate


def _target(*, xpts: float = 12.0, price: int = 50, team_id: int = 1) -> TransferTarget:
    player = SquadPlayer(
        fpl_id=16,
        player_code=1016,
        player_name="Incoming Forward",
        team_id=team_id,
        position="FWD",
        current_price_tenths=price,
        purchase_price_tenths=price,
        selling_price_tenths=price,
        squad_position=1,
        is_captain=False,
        is_vice_captain=False,
    )
    return TransferTarget(
        player=player,
        projection=PlayerGameweekProjection(fpl_id=16, expected_points=xpts, uncertainty=1.0),
    )


def test_recommends_best_legal_single_transfer_and_rescores_captaincy():
    recommendation = recommend_single_transfers(
        _validate(_players()),
        tuple(_projections()),
        (_target(),),
    )

    assert recommendation.recommended.outgoing.fpl_id == 11
    assert recommendation.recommended.incoming.fpl_id == 16
    assert recommendation.recommended.net_xpts_gain > 0
    assert recommendation.recommended.lineup.captain.fpl_id == 16
    assert recommendation.candidates_rejected_constraints == 2


def test_no_transfer_wins_when_a_hit_exceeds_the_projected_gain():
    squad = _validate(_players(), free_transfers=0, unlimited_transfers=False)
    recommendation = recommend_single_transfers(
        squad,
        tuple(_projections()),
        (_target(xpts=8.0),),
    )

    assert recommendation.recommended.is_no_transfer
    assert recommendation.transfer_alternatives[0].transfer_cost == 4.0
    assert recommendation.transfer_alternatives[0].net_xpts_gain < 0


def test_rejects_unaffordable_options_without_float_money_comparisons():
    recommendation = recommend_single_transfers(
        _validate(_players(), bank_tenths=0),
        tuple(_projections()),
        (_target(price=200),),
    )

    assert recommendation.recommended.is_no_transfer
    assert recommendation.transfer_alternatives == ()
    assert recommendation.candidates_rejected_budget == 3


def test_grandfathered_squad_only_accepts_a_transfer_that_restores_club_limit():
    players = _players()
    players[1] = replace(players[1], team_id=1)
    squad = _validate(players, allow_grandfathered_team_limit=True)
    target = _target(team_id=2)

    recommendation = recommend_single_transfers(squad, tuple(_projections()), (target,))

    assert all(
        option.outgoing.team_id == 1 for option in recommendation.transfer_alternatives
    )


def test_rejects_duplicate_or_mismatched_targets():
    target = _target()
    with pytest.raises(ValueError, match="duplicate transfer target"):
        recommend_single_transfers(
            _validate(_players()), tuple(_projections()), (target, target)
        )
    with pytest.raises(ValueError, match="projection mismatch"):
        recommend_single_transfers(
            _validate(_players()),
            tuple(_projections()),
            (replace(target, projection=replace(target.projection, fpl_id=17)),),
        )
