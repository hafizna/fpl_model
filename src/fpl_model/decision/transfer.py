"""Explainable no-transfer and single-transfer recommendation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite

from fpl_model.decision.lineup import (
    LineupRecommendation,
    PlayerGameweekProjection,
    recommend_lineup,
)
from fpl_model.decision.squad import SquadPlayer, ValidatedSquad, validate_squad

DEFAULT_HIT_COST = 4.0


@dataclass(frozen=True, slots=True)
class TransferTarget:
    """One non-owned player with a complete same-GW projection."""

    player: SquadPlayer
    projection: PlayerGameweekProjection


@dataclass(frozen=True, slots=True)
class TransferOption:
    outgoing: SquadPlayer | None
    incoming: SquadPlayer | None
    bank_after_tenths: int
    transfer_cost: float
    gross_xpts_gain: float
    net_xpts_gain: float
    lineup: LineupRecommendation

    @property
    def is_no_transfer(self) -> bool:
        return self.outgoing is None


@dataclass(frozen=True, slots=True)
class TransferRecommendation:
    recommended: TransferOption
    no_transfer: TransferOption
    transfer_alternatives: tuple[TransferOption, ...]
    candidates_considered: int
    candidates_rejected_budget: int
    candidates_rejected_constraints: int


def _replacement_squad(
    squad: ValidatedSquad,
    *,
    outgoing: SquadPlayer,
    incoming: SquadPlayer,
    bank_after_tenths: int,
) -> ValidatedSquad:
    replacement = replace(
        incoming,
        purchase_price_tenths=incoming.current_price_tenths,
        selling_price_tenths=incoming.current_price_tenths,
        squad_position=outgoing.squad_position,
        is_captain=outgoing.is_captain,
        is_vice_captain=outgoing.is_vice_captain,
    )
    players = tuple(replacement if row.fpl_id == outgoing.fpl_id else row for row in squad.players)
    return validate_squad(
        players,
        bank_tenths=bank_after_tenths,
        free_transfers=squad.free_transfers,
        unlimited_transfers=squad.unlimited_transfers,
        chip_period=squad.chip_period,
        chip_states=dict(squad.chip_states),
        allow_grandfathered_team_limit=False,
    )


def recommend_single_transfers(
    squad: ValidatedSquad,
    owned_projections: tuple[PlayerGameweekProjection, ...],
    targets: tuple[TransferTarget, ...],
    *,
    top_n: int = 10,
    hit_cost: float = DEFAULT_HIT_COST,
) -> TransferRecommendation:
    """Compare no transfer with every affordable, legal same-position swap.

    The result is intentionally single-Gameweek and single-transfer. Each legal
    post-transfer squad is rescored with its own optimal XI and captaincy.
    """
    if top_n <= 0:
        raise ValueError("top_n must be positive")
    if not isfinite(hit_cost) or hit_cost < 0:
        raise ValueError("hit_cost must be finite and non-negative")

    baseline_lineup = recommend_lineup(squad, owned_projections)
    no_transfer = TransferOption(
        outgoing=None,
        incoming=None,
        bank_after_tenths=squad.bank_tenths,
        transfer_cost=0.0,
        gross_xpts_gain=0.0,
        net_xpts_gain=0.0,
        lineup=baseline_lineup,
    )
    owned_ids = {player.fpl_id for player in squad.players}
    owned_projection_by_id = {row.fpl_id: row for row in owned_projections}
    if len(owned_projection_by_id) != 15:
        raise ValueError("owned_projections must contain exactly 15 unique squad players")

    transfer_cost = (
        0.0
        if squad.unlimited_transfers or (squad.free_transfers or 0) >= 1
        else float(hit_cost)
    )
    options: list[TransferOption] = []
    considered = 0
    rejected_budget = 0
    rejected_constraints = 0
    seen_targets: set[int] = set()
    for target in targets:
        incoming = target.player
        if incoming.fpl_id in seen_targets:
            raise ValueError(f"duplicate transfer target fpl_id: {incoming.fpl_id}")
        seen_targets.add(incoming.fpl_id)
        if incoming.fpl_id in owned_ids:
            continue
        if target.projection.fpl_id != incoming.fpl_id:
            raise ValueError(f"target projection mismatch for fpl_id {incoming.fpl_id}")

        for outgoing in squad.players:
            if outgoing.position != incoming.position:
                continue
            considered += 1
            bank_after = (
                squad.bank_tenths
                + outgoing.selling_price_tenths
                - incoming.current_price_tenths
            )
            if bank_after < 0:
                rejected_budget += 1
                continue
            try:
                candidate_squad = _replacement_squad(
                    squad,
                    outgoing=outgoing,
                    incoming=incoming,
                    bank_after_tenths=bank_after,
                )
            except ValueError:
                rejected_constraints += 1
                continue

            candidate_projections = tuple(
                target.projection if row.fpl_id == outgoing.fpl_id else row
                for row in owned_projections
            )
            lineup = recommend_lineup(candidate_squad, candidate_projections)
            gross_gain = lineup.total_xpts - baseline_lineup.total_xpts
            options.append(
                TransferOption(
                    outgoing=outgoing,
                    incoming=next(
                        row for row in candidate_squad.players if row.fpl_id == incoming.fpl_id
                    ),
                    bank_after_tenths=bank_after,
                    transfer_cost=transfer_cost,
                    gross_xpts_gain=gross_gain,
                    net_xpts_gain=gross_gain - transfer_cost,
                    lineup=lineup,
                )
            )

    options.sort(
        key=lambda option: (
            -option.net_xpts_gain,
            -option.gross_xpts_gain,
            option.incoming.fpl_id if option.incoming is not None else 0,
            option.outgoing.fpl_id if option.outgoing is not None else 0,
        )
    )
    alternatives = tuple(options[:top_n])
    recommended = no_transfer
    if alternatives and alternatives[0].net_xpts_gain > 0:
        recommended = alternatives[0]
    return TransferRecommendation(
        recommended=recommended,
        no_transfer=no_transfer,
        transfer_alternatives=alternatives,
        candidates_considered=considered,
        candidates_rejected_budget=rejected_budget,
        candidates_rejected_constraints=rejected_constraints,
    )
