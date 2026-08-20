from __future__ import annotations

from dataclasses import replace

import pytest

from fpl_model.decision.squad import SquadPlayer, validate_squad


def _players() -> list[SquadPlayer]:
    positions = (
        "GK",
        "DEF",
        "DEF",
        "DEF",
        "MID",
        "MID",
        "MID",
        "MID",
        "FWD",
        "FWD",
        "FWD",
        "GK",
        "DEF",
        "DEF",
        "MID",
    )
    return [
        SquadPlayer(
            fpl_id=index,
            player_code=1000 + index,
            player_name=f"Player {index}",
            team_id=((index - 1) % 5) + 1,
            position=position,
            current_price_tenths=50 + index,
            purchase_price_tenths=50 + index,
            selling_price_tenths=50 + index,
            squad_position=index,
            is_captain=index == 9,
            is_vice_captain=index == 5,
        )
        for index, position in enumerate(positions, start=1)
    ]


def _validate(players: list[SquadPlayer], **overrides):
    arguments = {
        "bank_tenths": 5,
        "free_transfers": 2,
        "unlimited_transfers": False,
        "chip_period": 1,
        "chip_states": {
            "wildcard": "available",
            "free_hit": "available",
            "bench_boost": "available",
            "triple_captain": "available",
        },
    }
    arguments.update(overrides)
    return validate_squad(players, **arguments)


def test_validates_complete_squad_and_calculates_liquid_team_value():
    squad = _validate(_players())

    assert [player.squad_position for player in squad.players] == list(range(1, 16))
    assert squad.team_value_tenths == 5 + sum(range(51, 66))


def test_rejects_wrong_position_composition():
    players = _players()
    players[-1] = replace(players[-1], position="DEF")

    with pytest.raises(ValueError, match="position counts"):
        _validate(players)


def test_rejects_more_than_three_players_from_one_club():
    players = _players()
    players[1] = replace(players[1], team_id=1)

    with pytest.raises(ValueError, match="three-player club limit"):
        _validate(players)


def test_can_preserve_grandfathered_team_limit_as_an_explicit_constraint_flag():
    players = _players()
    players[1] = replace(players[1], team_id=1)

    squad = _validate(players, allow_grandfathered_team_limit=True)

    assert squad.constraint_flags == ("GRANDFATHERED_TEAM_LIMIT:team_id=1",)


def test_rejects_invalid_starting_formation():
    players = _players()
    players[3] = replace(players[3], squad_position=15)
    players[-1] = replace(players[-1], squad_position=4)

    with pytest.raises(ValueError, match="at least three defenders"):
        _validate(players)


def test_rejects_captain_on_the_bench():
    players = _players()
    players[8] = replace(players[8], squad_position=13)
    players[12] = replace(players[12], squad_position=9)

    with pytest.raises(ValueError, match="captain and vice-captain must both"):
        _validate(players)


def test_unlimited_transfer_state_requires_blank_free_transfer_count():
    with pytest.raises(ValueError, match="must be blank"):
        _validate(_players(), unlimited_transfers=True, free_transfers=5)

    squad = _validate(_players(), unlimited_transfers=True, free_transfers=None)
    assert squad.unlimited_transfers is True
    assert squad.free_transfers is None


def test_rejects_missing_or_multiple_active_chip_state():
    with pytest.raises(ValueError, match="chip_states must contain exactly"):
        _validate(_players(), chip_states={"wildcard": "available"})

    with pytest.raises(ValueError, match="only one chip"):
        _validate(
            _players(),
            chip_states={
                "wildcard": "active",
                "free_hit": "active",
                "bench_boost": "available",
                "triple_captain": "available",
            },
        )


def test_active_transfer_chip_requires_unlimited_transfer_state():
    with pytest.raises(ValueError, match="requires unlimited_transfers"):
        _validate(
            _players(),
            chip_states={
                "wildcard": "active",
                "free_hit": "available",
                "bench_boost": "available",
                "triple_captain": "available",
            },
        )
