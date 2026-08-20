"""Canonical FPL squad state and deterministic rules validation.

Money is represented in integer tenths of a million throughout the decision
layer.  This avoids floating-point budget errors when a later transfer planner
compares sale proceeds, bank, and incoming-player prices.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

POSITION_COUNTS = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
CHIP_NAMES = ("wildcard", "free_hit", "bench_boost", "triple_captain")
CHIP_STATUSES = ("available", "active", "played", "expired")
MAX_FREE_TRANSFERS = 5


@dataclass(frozen=True, slots=True)
class SquadPlayer:
    fpl_id: int
    player_code: int | None
    player_name: str
    team_id: int
    position: str
    current_price_tenths: int
    purchase_price_tenths: int
    selling_price_tenths: int
    squad_position: int
    is_captain: bool
    is_vice_captain: bool


@dataclass(frozen=True, slots=True)
class ValidatedSquad:
    players: tuple[SquadPlayer, ...]
    bank_tenths: int
    free_transfers: int | None
    unlimited_transfers: bool
    chip_period: int
    chip_states: tuple[tuple[str, str], ...]
    constraint_flags: tuple[str, ...]

    @property
    def team_value_tenths(self) -> int:
        """Current liquid value: sale proceeds for all players plus bank."""
        return self.bank_tenths + sum(player.selling_price_tenths for player in self.players)


def validate_squad(
    players: Iterable[SquadPlayer],
    *,
    bank_tenths: int,
    free_transfers: int | None,
    unlimited_transfers: bool,
    chip_period: int,
    chip_states: Mapping[str, str],
    allow_grandfathered_team_limit: bool = False,
) -> ValidatedSquad:
    """Validate one complete 15-player manager snapshot.

    The squad and lineup constraints are the official 2026/27 FPL rules:
    2 GK, 5 DEF, 5 MID, 3 FWD; no more than three players from one club;
    and an XI containing one goalkeeper, at least three defenders, and at
    least one forward.  ``squad_position`` 1..11 denotes the starting XI.
    """
    canonical = tuple(sorted(players, key=lambda player: player.squad_position))
    if len(canonical) != 15:
        raise ValueError(f"squad must contain exactly 15 players, got {len(canonical)}")

    fpl_ids = [player.fpl_id for player in canonical]
    if any(fpl_id <= 0 for fpl_id in fpl_ids):
        raise ValueError("fpl_id must be positive")
    if len(set(fpl_ids)) != len(fpl_ids):
        raise ValueError("squad contains duplicate fpl_id values")

    squad_positions = [player.squad_position for player in canonical]
    if sorted(squad_positions) != list(range(1, 16)):
        raise ValueError("squad_position must contain every integer from 1 through 15 exactly once")

    position_counts = Counter(player.position for player in canonical)
    if dict(position_counts) != POSITION_COUNTS:
        raise ValueError(
            f"squad position counts must be {POSITION_COUNTS}, got {dict(position_counts)}"
        )

    team_counts = Counter(player.team_id for player in canonical)
    over_limit = sorted(team_id for team_id, count in team_counts.items() if count > 3)
    if over_limit and not allow_grandfathered_team_limit:
        raise ValueError(f"squad exceeds three-player club limit for team_id: {over_limit}")
    constraint_flags = tuple(
        f"GRANDFATHERED_TEAM_LIMIT:team_id={team_id}" for team_id in over_limit
    )

    for player in canonical:
        if not player.player_name.strip():
            raise ValueError(f"player {player.fpl_id} has a blank player_name")
        if player.team_id <= 0:
            raise ValueError(f"player {player.fpl_id} has a non-positive team_id")
        if player.current_price_tenths <= 0 or player.purchase_price_tenths <= 0:
            raise ValueError(f"player {player.fpl_id} prices must be positive")
        if player.selling_price_tenths <= 0:
            raise ValueError(f"player {player.fpl_id} selling price must be positive")
        if player.selling_price_tenths > player.current_price_tenths:
            raise ValueError(
                f"player {player.fpl_id} selling price cannot exceed current market price"
            )
        if player.is_captain and player.is_vice_captain:
            raise ValueError(f"player {player.fpl_id} cannot be captain and vice-captain")

    captains = [player for player in canonical if player.is_captain]
    vice_captains = [player for player in canonical if player.is_vice_captain]
    if len(captains) != 1 or len(vice_captains) != 1:
        raise ValueError("squad must contain exactly one captain and one vice-captain")
    if captains[0].squad_position > 11 or vice_captains[0].squad_position > 11:
        raise ValueError("captain and vice-captain must both be in the starting XI")

    starters = canonical[:11]
    starter_counts = Counter(player.position for player in starters)
    if starter_counts["GK"] != 1:
        raise ValueError("starting XI must contain exactly one goalkeeper")
    if starter_counts["DEF"] < 3:
        raise ValueError("starting XI must contain at least three defenders")
    if starter_counts["FWD"] < 1:
        raise ValueError("starting XI must contain at least one forward")

    if bank_tenths < 0:
        raise ValueError("bank_tenths must be non-negative")
    if unlimited_transfers:
        if free_transfers is not None:
            raise ValueError("free_transfers must be blank when unlimited_transfers is true")
    elif free_transfers is None or not 0 <= free_transfers <= MAX_FREE_TRANSFERS:
        raise ValueError(f"free_transfers must be between 0 and {MAX_FREE_TRANSFERS}")

    if chip_period not in (1, 2):
        raise ValueError("chip_period must be 1 or 2")
    if set(chip_states) != set(CHIP_NAMES):
        raise ValueError(f"chip_states must contain exactly: {', '.join(CHIP_NAMES)}")
    invalid_chip_states = {
        chip: status for chip, status in chip_states.items() if status not in CHIP_STATUSES
    }
    if invalid_chip_states:
        raise ValueError(f"invalid chip states: {invalid_chip_states}")
    active_chips = [chip for chip, status in chip_states.items() if status == "active"]
    if len(active_chips) > 1:
        raise ValueError("only one chip may be active in a gameweek")
    if active_chips and active_chips[0] in {"wildcard", "free_hit"} and not unlimited_transfers:
        raise ValueError(f"active {active_chips[0]} requires unlimited_transfers")

    return ValidatedSquad(
        players=canonical,
        bank_tenths=bank_tenths,
        free_transfers=free_transfers,
        unlimited_transfers=unlimited_transfers,
        chip_period=chip_period,
        chip_states=tuple(sorted(chip_states.items())),
        constraint_flags=constraint_flags,
    )
