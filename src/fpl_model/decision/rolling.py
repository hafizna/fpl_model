"""Transparent rolling three-Gameweek transfer planning."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from fpl_model.decision.lineup import LineupRecommendation, recommend_lineup
from fpl_model.decision.squad import MAX_FREE_TRANSFERS, ValidatedSquad, validate_squad
from fpl_model.decision.transfer import (
    DEFAULT_HIT_COST,
    TransferTarget,
    apply_single_transfer,
    recommend_single_transfers,
)

PLANNING_HORIZON_GAMEWEEKS = 3
DEFAULT_BEAM_WIDTH = 30
DEFAULT_CANDIDATES_PER_POSITION = 6
DEFAULT_RETURNED_PLANS = 5


@dataclass(frozen=True, slots=True)
class GameweekProjectionPool:
    gameweek: int
    players: tuple[TransferTarget, ...]
    transferable_fpl_ids: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class RollingPlanStep:
    gameweek: int
    outgoing_fpl_id: int | None
    incoming_fpl_id: int | None
    free_transfers_before: int
    free_transfers_after: int
    bank_after_tenths: int
    transfer_cost: float
    lineup: LineupRecommendation
    net_gameweek_xpts: float

    @property
    def decision(self) -> str:
        return "roll" if self.outgoing_fpl_id is None else "transfer"


@dataclass(frozen=True, slots=True)
class RollingPlan:
    steps: tuple[RollingPlanStep, ...]
    cumulative_net_xpts: float
    total_transfer_cost: float
    terminal_bank_tenths: int
    terminal_free_transfers: int
    uncertainty: float | None
    data_quality_flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RollingPlannerResult:
    recommended: RollingPlan
    alternatives: tuple[RollingPlan, ...]
    eligible_player_ids: tuple[int, ...]
    beam_width: int
    candidates_per_position: int
    search_is_exact: bool = False


@dataclass(frozen=True, slots=True)
class _SearchState:
    squad: ValidatedSquad
    steps: tuple[RollingPlanStep, ...]
    cumulative_net_xpts: float
    total_transfer_cost: float
    uncertainty_variance: float | None
    flags: frozenset[str]
    ranking_score: float


def _pool_by_id(pool: GameweekProjectionPool) -> dict[int, TransferTarget]:
    result = {row.player.fpl_id: row for row in pool.players}
    if len(result) != len(pool.players):
        raise ValueError(f"GW{pool.gameweek} projection pool contains duplicate fpl_id values")
    for fpl_id, target in result.items():
        if target.projection.fpl_id != fpl_id:
            raise ValueError(f"GW{pool.gameweek} projection mismatch for fpl_id {fpl_id}")
    return result


def _validate_pools(
    squad: ValidatedSquad,
    pools: tuple[GameweekProjectionPool, ...],
) -> tuple[dict[int, TransferTarget], ...]:
    if len(pools) != PLANNING_HORIZON_GAMEWEEKS:
        raise ValueError("rolling planner requires exactly three Gameweek projection pools")
    expected = tuple(range(pools[0].gameweek, pools[0].gameweek + 3))
    actual = tuple(pool.gameweek for pool in pools)
    if actual != expected:
        raise ValueError(f"projection pools must be consecutive, expected {expected}, got {actual}")
    if squad.unlimited_transfers:
        raise ValueError("rolling planner does not yet support Wildcard or Free Hit state")
    if squad.free_transfers is None:
        raise ValueError("rolling planner requires an explicit free-transfer count")
    if any(status == "active" for _, status in squad.chip_states):
        raise ValueError("rolling planner does not yet support an active chip")

    maps = tuple(_pool_by_id(pool) for pool in pools)
    for pool, rows in zip(pools, maps, strict=True):
        if pool.transferable_fpl_ids is not None:
            unknown = sorted(set(pool.transferable_fpl_ids) - set(rows))
            if unknown:
                raise ValueError(
                    f"GW{pool.gameweek} transferable IDs lack projections: {unknown}"
                )
    owned_ids = {player.fpl_id for player in squad.players}
    for pool, rows in zip(pools, maps, strict=True):
        missing = sorted(owned_ids - set(rows))
        if missing:
            raise ValueError(f"GW{pool.gameweek} is missing squad projections: {missing}")

    common_ids = set.intersection(*(set(rows) for rows in maps))
    for fpl_id in common_ids:
        identities = {
            (
                rows[fpl_id].player.team_id,
                rows[fpl_id].player.position,
                rows[fpl_id].player.current_price_tenths,
            )
            for rows in maps
        }
        if len(identities) != 1:
            raise ValueError(f"player identity or frozen price changes inside horizon: {fpl_id}")
    return maps


def _advance_free_transfers(squad: ValidatedSquad, *, transfers_used: int) -> ValidatedSquad:
    if squad.free_transfers is None:
        raise ValueError("free-transfer state is missing")
    next_free_transfers = min(
        MAX_FREE_TRANSFERS,
        max(0, squad.free_transfers - transfers_used) + 1,
    )
    return validate_squad(
        squad.players,
        bank_tenths=squad.bank_tenths,
        free_transfers=next_free_transfers,
        unlimited_transfers=False,
        chip_period=squad.chip_period,
        chip_states=dict(squad.chip_states),
        allow_grandfathered_team_limit=bool(squad.constraint_flags),
    )


def _lineup_for_squad(
    squad: ValidatedSquad,
    rows: dict[int, TransferTarget],
) -> LineupRecommendation:
    return recommend_lineup(
        squad,
        tuple(rows[player.fpl_id].projection for player in squad.players),
    )


def _future_hold_score(
    squad: ValidatedSquad,
    future_maps: tuple[dict[int, TransferTarget], ...],
) -> float:
    return sum(_lineup_for_squad(squad, rows).total_xpts for rows in future_maps)


def _state_key(state: _SearchState) -> tuple[object, ...]:
    return (
        tuple(
            sorted(
                (
                    player.fpl_id,
                    player.purchase_price_tenths,
                    player.selling_price_tenths,
                )
                for player in state.squad.players
            )
        ),
        state.squad.bank_tenths,
        state.squad.free_transfers,
    )


def _state_sort_key(state: _SearchState) -> tuple[object, ...]:
    decisions = tuple(
        (
            step.outgoing_fpl_id or 0,
            step.incoming_fpl_id or 0,
        )
        for step in state.steps
    )
    return (-state.ranking_score, -state.cumulative_net_xpts, state.total_transfer_cost, decisions)


def _candidate_targets(
    *,
    index: int,
    maps: tuple[dict[int, TransferTarget], ...],
    common_ids: set[int],
    owned_ids: set[int],
    transferable_ids: set[int],
    per_position: int,
) -> tuple[TransferTarget, ...]:
    scores = {
        fpl_id: sum(maps[future][fpl_id].projection.expected_points for future in range(index, 3))
        for fpl_id in (common_ids - owned_ids) & transferable_ids
    }
    result: list[TransferTarget] = []
    for position in ("GK", "DEF", "MID", "FWD"):
        position_ids = [
            fpl_id
            for fpl_id in scores
            if maps[index][fpl_id].player.position == position
        ]
        position_ids.sort(key=lambda fpl_id: (-scores[fpl_id], fpl_id))
        result.extend(maps[index][fpl_id] for fpl_id in position_ids[:per_position])
    return tuple(result)


def plan_three_gameweeks(
    squad: ValidatedSquad,
    pools: tuple[GameweekProjectionPool, ...],
    *,
    beam_width: int = DEFAULT_BEAM_WIDTH,
    candidates_per_position: int = DEFAULT_CANDIDATES_PER_POSITION,
    returned_plans: int = DEFAULT_RETURNED_PLANS,
    hit_cost: float = DEFAULT_HIT_COST,
) -> RollingPlannerResult:
    """Search no-transfer/single-transfer paths over three consecutive GWs.

    Candidate pruning and beam search make this intentionally approximate.
    Every retained state still obeys exact FPL squad, budget, and FT rules.
    """
    if beam_width <= 0 or candidates_per_position <= 0 or returned_plans <= 0:
        raise ValueError("beam_width, candidates_per_position, and returned_plans must be positive")
    maps = _validate_pools(squad, pools)
    common_ids = set.intersection(*(set(rows) for rows in maps))
    initial = _SearchState(
        squad=squad,
        steps=(),
        cumulative_net_xpts=0.0,
        total_transfer_cost=0.0,
        uncertainty_variance=0.0,
        flags=frozenset(),
        ranking_score=_future_hold_score(squad, maps),
    )
    states = [initial]

    for index, pool in enumerate(pools):
        expanded: list[_SearchState] = []
        future_maps = maps[index + 1 :]
        for state in states:
            current_rows = maps[index]
            lineup = _lineup_for_squad(state.squad, current_rows)
            advanced = _advance_free_transfers(state.squad, transfers_used=0)
            variance = (
                None
                if state.uncertainty_variance is None or lineup.uncertainty is None
                else state.uncertainty_variance + lineup.uncertainty**2
            )
            roll_step = RollingPlanStep(
                gameweek=pool.gameweek,
                outgoing_fpl_id=None,
                incoming_fpl_id=None,
                free_transfers_before=int(state.squad.free_transfers),
                free_transfers_after=int(advanced.free_transfers),
                bank_after_tenths=advanced.bank_tenths,
                transfer_cost=0.0,
                lineup=lineup,
                net_gameweek_xpts=lineup.total_xpts,
            )
            roll_total = state.cumulative_net_xpts + lineup.total_xpts
            expanded.append(
                _SearchState(
                    squad=advanced,
                    steps=(*state.steps, roll_step),
                    cumulative_net_xpts=roll_total,
                    total_transfer_cost=state.total_transfer_cost,
                    uncertainty_variance=variance,
                    flags=state.flags | frozenset(lineup.data_quality_flags),
                    ranking_score=roll_total + _future_hold_score(advanced, future_maps),
                )
            )

            targets = _candidate_targets(
                index=index,
                maps=maps,
                common_ids=common_ids,
                owned_ids={player.fpl_id for player in state.squad.players},
                transferable_ids=(
                    set(current_rows)
                    if pool.transferable_fpl_ids is None
                    else set(pool.transferable_fpl_ids)
                ),
                per_position=candidates_per_position,
            )
            max_options = sum(
                sum(player.position == target.player.position for player in state.squad.players)
                for target in targets
            )
            if max_options == 0:
                continue
            recommendation = recommend_single_transfers(
                state.squad,
                tuple(current_rows[player.fpl_id].projection for player in state.squad.players),
                targets,
                top_n=max_options,
                hit_cost=hit_cost,
            )
            for option in recommendation.transfer_alternatives:
                if option.outgoing is None or option.incoming is None:
                    continue
                transferred = apply_single_transfer(
                    state.squad,
                    outgoing=option.outgoing,
                    incoming=option.incoming,
                    bank_after_tenths=option.bank_after_tenths,
                )
                advanced = _advance_free_transfers(transferred, transfers_used=1)
                step_variance = option.lineup.uncertainty
                variance = (
                    None
                    if state.uncertainty_variance is None or step_variance is None
                    else state.uncertainty_variance + step_variance**2
                )
                step = RollingPlanStep(
                    gameweek=pool.gameweek,
                    outgoing_fpl_id=option.outgoing.fpl_id,
                    incoming_fpl_id=option.incoming.fpl_id,
                    free_transfers_before=int(state.squad.free_transfers),
                    free_transfers_after=int(advanced.free_transfers),
                    bank_after_tenths=advanced.bank_tenths,
                    transfer_cost=option.transfer_cost,
                    lineup=option.lineup,
                    net_gameweek_xpts=option.lineup.total_xpts - option.transfer_cost,
                )
                cumulative = state.cumulative_net_xpts + step.net_gameweek_xpts
                expanded.append(
                    _SearchState(
                        squad=advanced,
                        steps=(*state.steps, step),
                        cumulative_net_xpts=cumulative,
                        total_transfer_cost=state.total_transfer_cost + option.transfer_cost,
                        uncertainty_variance=variance,
                        flags=state.flags | frozenset(option.lineup.data_quality_flags),
                        ranking_score=cumulative + _future_hold_score(advanced, future_maps),
                    )
                )

        best_by_state: dict[tuple[object, ...], _SearchState] = {}
        for state in sorted(expanded, key=_state_sort_key):
            best_by_state.setdefault(_state_key(state), state)
        states = sorted(best_by_state.values(), key=_state_sort_key)[:beam_width]

    states.sort(key=lambda state: (-state.cumulative_net_xpts, state.total_transfer_cost, _state_sort_key(state)))
    plans = tuple(
        RollingPlan(
            steps=state.steps,
            cumulative_net_xpts=state.cumulative_net_xpts,
            total_transfer_cost=state.total_transfer_cost,
            terminal_bank_tenths=state.squad.bank_tenths,
            terminal_free_transfers=int(state.squad.free_transfers),
            uncertainty=(
                None
                if state.uncertainty_variance is None
                else sqrt(state.uncertainty_variance)
            ),
            data_quality_flags=tuple(sorted(state.flags)),
        )
        for state in states[:returned_plans]
    )
    if not plans:
        raise ValueError("rolling planner found no feasible plan")
    return RollingPlannerResult(
        recommended=plans[0],
        alternatives=plans[1:],
        eligible_player_ids=tuple(sorted(common_ids)),
        beam_width=beam_width,
        candidates_per_position=candidates_per_position,
    )
