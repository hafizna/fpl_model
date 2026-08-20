"""Approximate, explainable preseason initial-squad optimization."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from math import isfinite, sqrt

from fpl_model.decision.lineup import LineupRecommendation, recommend_lineup
from fpl_model.decision.rolling import GameweekProjectionPool
from fpl_model.decision.squad import POSITION_COUNTS, SquadPlayer, ValidatedSquad, validate_squad
from fpl_model.decision.transfer import TransferTarget

DEFAULT_INITIAL_BUDGET_TENTHS = 1_000
DEFAULT_INITIAL_SQUAD_BEAM_WIDTH = 2_000
DEFAULT_CANDIDATES_PER_POSITION_PER_LENS = 10
DEFAULT_RETURNED_INITIAL_SQUADS = 5

_POSITION_ORDER = ("FWD",) * 3 + ("MID",) * 5 + ("DEF",) * 5 + ("GK",) * 2
_AVAILABLE_CHIPS = {
    "wildcard": "available",
    "free_hit": "available",
    "bench_boost": "available",
    "triple_captain": "available",
}


@dataclass(frozen=True, slots=True)
class InitialSquadGameweek:
    gameweek: int
    lineup: LineupRecommendation


@dataclass(frozen=True, slots=True)
class InitialSquadPlan:
    squad: ValidatedSquad
    gameweeks: tuple[InitialSquadGameweek, ...]
    squad_cost_tenths: int
    bank_tenths: int
    cumulative_xpts: float
    uncertainty: float | None
    data_quality_flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InitialSquadOptimizerResult:
    recommended: InitialSquadPlan
    alternatives: tuple[InitialSquadPlan, ...]
    eligible_player_ids: tuple[int, ...]
    candidate_player_ids: tuple[int, ...]
    complete_squads_evaluated: int
    beam_width: int
    candidates_per_position_per_lens: int
    search_is_exact: bool = False


@dataclass(frozen=True, slots=True)
class _PartialSquad:
    player_ids: tuple[int, ...]
    cost_tenths: int
    proxy_xpts: float


def _pool_by_id(pool: GameweekProjectionPool) -> dict[int, TransferTarget]:
    rows = {row.player.fpl_id: row for row in pool.players}
    if len(rows) != len(pool.players):
        raise ValueError(f"GW{pool.gameweek} projection pool contains duplicate fpl_id values")
    for fpl_id, row in rows.items():
        if row.projection.fpl_id != fpl_id:
            raise ValueError(f"GW{pool.gameweek} projection mismatch for fpl_id {fpl_id}")
    return rows


def _validate_pools(
    pools: tuple[GameweekProjectionPool, ...],
) -> tuple[tuple[dict[int, TransferTarget], ...], set[int]]:
    if len(pools) != 3:
        raise ValueError("initial-squad optimizer requires exactly three Gameweek pools")
    expected = tuple(range(pools[0].gameweek, pools[0].gameweek + 3))
    actual = tuple(pool.gameweek for pool in pools)
    if actual != expected:
        raise ValueError(f"projection pools must be consecutive, expected {expected}, got {actual}")

    maps = tuple(_pool_by_id(pool) for pool in pools)
    transferable_sets = []
    for pool, rows in zip(pools, maps, strict=True):
        transferable = set(rows) if pool.transferable_fpl_ids is None else set(pool.transferable_fpl_ids)
        unknown = sorted(transferable - set(rows))
        if unknown:
            raise ValueError(f"GW{pool.gameweek} transferable IDs lack projections: {unknown}")
        transferable_sets.append(transferable)
    common_ids = set.intersection(*(set(rows) for rows in maps), *transferable_sets)

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
    for position, required in POSITION_COUNTS.items():
        available = sum(maps[0][fpl_id].player.position == position for fpl_id in common_ids)
        if available < required:
            raise ValueError(
                f"not enough fully projected transferable {position} players: "
                f"required={required}, available={available}"
            )
    return maps, common_ids


def _candidate_ids(
    maps: tuple[dict[int, TransferTarget], ...],
    common_ids: set[int],
    *,
    per_lens: int,
) -> tuple[int, ...]:
    horizon_score = {
        fpl_id: sum(rows[fpl_id].projection.expected_points for rows in maps)
        for fpl_id in common_ids
    }
    selected: set[int] = set()
    for position, required in POSITION_COUNTS.items():
        position_ids = [
            fpl_id
            for fpl_id in common_ids
            if maps[0][fpl_id].player.position == position
        ]
        by_score = sorted(position_ids, key=lambda value: (-horizon_score[value], value))
        by_value = sorted(
            position_ids,
            key=lambda value: (
                -horizon_score[value] / maps[0][value].player.current_price_tenths,
                -horizon_score[value],
                value,
            ),
        )
        by_price = sorted(
            position_ids,
            key=lambda value: (
                maps[0][value].player.current_price_tenths,
                -horizon_score[value],
                value,
            ),
        )
        selected.update(by_score[:per_lens])
        selected.update(by_value[:per_lens])
        # Explicit cheap enablers prevent score/value pruning from making an
        # otherwise feasible £100m squad impossible before beam search starts.
        selected.update(by_price[:required])
    return tuple(sorted(selected))


def _minimum_remaining_cost(
    *,
    selected_ids: tuple[int, ...],
    candidate_ids_by_position: dict[str, tuple[int, ...]],
    players: dict[int, SquadPlayer],
) -> int | None:
    selected_counts = Counter(players[fpl_id].position for fpl_id in selected_ids)
    selected = set(selected_ids)
    total = 0
    for position, required in POSITION_COUNTS.items():
        remaining = required - selected_counts[position]
        if remaining <= 0:
            continue
        prices = sorted(
            players[fpl_id].current_price_tenths
            for fpl_id in candidate_ids_by_position[position]
            if fpl_id not in selected
        )
        if len(prices) < remaining:
            return None
        total += sum(prices[:remaining])
    return total


def _provisional_squad(
    selected_ids: tuple[int, ...],
    *,
    players: dict[int, SquadPlayer],
    first_gameweek_xpts: dict[int, float],
    budget_tenths: int,
) -> ValidatedSquad:
    by_position = {
        position: sorted(
            (players[fpl_id] for fpl_id in selected_ids if players[fpl_id].position == position),
            key=lambda player: (-first_gameweek_xpts[player.fpl_id], player.fpl_id),
        )
        for position in POSITION_COUNTS
    }
    starters = (
        by_position["GK"][:1]
        + by_position["DEF"][:3]
        + by_position["MID"][:4]
        + by_position["FWD"][:3]
    )
    bench = (
        by_position["GK"][1:]
        + by_position["DEF"][3:]
        + by_position["MID"][4:]
    )
    captain_order = sorted(
        starters,
        key=lambda player: (-first_gameweek_xpts[player.fpl_id], player.fpl_id),
    )
    captain_id, vice_id = (player.fpl_id for player in captain_order[:2])
    ordered = (*starters, *bench)
    canonical = tuple(
        replace(
            player,
            purchase_price_tenths=player.current_price_tenths,
            selling_price_tenths=player.current_price_tenths,
            squad_position=index,
            is_captain=player.fpl_id == captain_id,
            is_vice_captain=player.fpl_id == vice_id,
        )
        for index, player in enumerate(ordered, start=1)
    )
    cost = sum(player.current_price_tenths for player in canonical)
    return validate_squad(
        canonical,
        bank_tenths=budget_tenths - cost,
        free_transfers=None,
        unlimited_transfers=True,
        chip_period=1,
        chip_states=_AVAILABLE_CHIPS,
    )


def _align_to_first_lineup(
    squad: ValidatedSquad,
    lineup: LineupRecommendation,
) -> ValidatedSquad:
    ordered = (
        *lineup.starters,
        lineup.bench_goalkeeper,
        *lineup.outfield_bench_order,
    )
    players = tuple(
        replace(
            player,
            squad_position=index,
            is_captain=player.fpl_id == lineup.captain.fpl_id,
            is_vice_captain=player.fpl_id == lineup.vice_captain.fpl_id,
        )
        for index, player in enumerate(ordered, start=1)
    )
    return validate_squad(
        players,
        bank_tenths=squad.bank_tenths,
        free_transfers=None,
        unlimited_transfers=True,
        chip_period=1,
        chip_states=_AVAILABLE_CHIPS,
    )


def _evaluate_squad(
    selected_ids: tuple[int, ...],
    *,
    maps: tuple[dict[int, TransferTarget], ...],
    players: dict[int, SquadPlayer],
    gameweeks: tuple[int, ...],
    budget_tenths: int,
) -> InitialSquadPlan:
    first_xpts = {
        fpl_id: maps[0][fpl_id].projection.expected_points for fpl_id in selected_ids
    }
    squad = _provisional_squad(
        selected_ids,
        players=players,
        first_gameweek_xpts=first_xpts,
        budget_tenths=budget_tenths,
    )

    def lineups_for(current: ValidatedSquad) -> tuple[InitialSquadGameweek, ...]:
        return tuple(
            InitialSquadGameweek(
                gameweek=gameweek,
                lineup=recommend_lineup(
                    current,
                    tuple(rows[player.fpl_id].projection for player in current.players),
                ),
            )
            for gameweek, rows in zip(gameweeks, maps, strict=True)
        )

    gameweek_rows = lineups_for(squad)
    squad = _align_to_first_lineup(squad, gameweek_rows[0].lineup)
    gameweek_rows = lineups_for(squad)
    uncertainties = [row.lineup.uncertainty for row in gameweek_rows]
    combined_uncertainty = (
        None
        if any(value is None for value in uncertainties)
        else sqrt(sum(value**2 for value in uncertainties if value is not None))
    )
    cost = sum(player.current_price_tenths for player in squad.players)
    flags = tuple(
        sorted({flag for row in gameweek_rows for flag in row.lineup.data_quality_flags})
    )
    return InitialSquadPlan(
        squad=squad,
        gameweeks=gameweek_rows,
        squad_cost_tenths=cost,
        bank_tenths=budget_tenths - cost,
        cumulative_xpts=sum(row.lineup.total_xpts for row in gameweek_rows),
        uncertainty=combined_uncertainty,
        data_quality_flags=flags,
    )


def optimize_initial_squad(
    pools: tuple[GameweekProjectionPool, ...],
    *,
    budget_tenths: int = DEFAULT_INITIAL_BUDGET_TENTHS,
    beam_width: int = DEFAULT_INITIAL_SQUAD_BEAM_WIDTH,
    candidates_per_position_per_lens: int = DEFAULT_CANDIDATES_PER_POSITION_PER_LENS,
    returned_squads: int = DEFAULT_RETURNED_INITIAL_SQUADS,
) -> InitialSquadOptimizerResult:
    """Select a legal initial squad and re-optimize XI/captain in each of three GWs.

    Candidate pruning uses three transparent lenses (horizon xPts, xPts per
    price, and cheapest enablers), followed by a bounded beam. Completed squads
    are ranked by exact three-GW lineup-plus-captain xPts. The result is an
    auditable approximate search, not a certified global optimum.
    """
    if budget_tenths <= 0:
        raise ValueError("budget_tenths must be positive")
    if beam_width <= 0 or candidates_per_position_per_lens <= 0 or returned_squads <= 0:
        raise ValueError("beam width, candidate limit, and returned squads must be positive")
    maps, common_ids = _validate_pools(pools)
    candidate_ids = _candidate_ids(
        maps,
        common_ids,
        per_lens=candidates_per_position_per_lens,
    )
    players = {fpl_id: maps[0][fpl_id].player for fpl_id in candidate_ids}
    candidate_ids_by_position = {
        position: tuple(
            sorted(fpl_id for fpl_id in candidate_ids if players[fpl_id].position == position)
        )
        for position in POSITION_COUNTS
    }
    horizon_score = {
        fpl_id: sum(rows[fpl_id].projection.expected_points for rows in maps)
        for fpl_id in candidate_ids
    }

    states = [_PartialSquad(player_ids=(), cost_tenths=0, proxy_xpts=0.0)]
    for position in _POSITION_ORDER:
        expanded: list[_PartialSquad] = []
        for state in states:
            selected = set(state.player_ids)
            team_counts = Counter(players[fpl_id].team_id for fpl_id in state.player_ids)
            same_position = [
                fpl_id for fpl_id in state.player_ids if players[fpl_id].position == position
            ]
            last_id = max(same_position, default=0)
            for fpl_id in candidate_ids_by_position[position]:
                if fpl_id <= last_id or fpl_id in selected:
                    continue
                player = players[fpl_id]
                if team_counts[player.team_id] >= 3:
                    continue
                new_ids = (*state.player_ids, fpl_id)
                new_cost = state.cost_tenths + player.current_price_tenths
                minimum_remaining = _minimum_remaining_cost(
                    selected_ids=new_ids,
                    candidate_ids_by_position=candidate_ids_by_position,
                    players=players,
                )
                if minimum_remaining is None or new_cost + minimum_remaining > budget_tenths:
                    continue
                expanded.append(
                    _PartialSquad(
                        player_ids=new_ids,
                        cost_tenths=new_cost,
                        proxy_xpts=state.proxy_xpts + horizon_score[fpl_id],
                    )
                )
        if not expanded:
            raise ValueError(
                "candidate-pruned search found no budget- and club-legal initial squad"
            )
        expanded.sort(key=lambda state: (-state.proxy_xpts, state.cost_tenths, state.player_ids))
        states = expanded[:beam_width]

    plans = [
        _evaluate_squad(
            state.player_ids,
            maps=maps,
            players=players,
            gameweeks=tuple(pool.gameweek for pool in pools),
            budget_tenths=budget_tenths,
        )
        for state in states
    ]
    plans.sort(
        key=lambda plan: (
            -plan.cumulative_xpts,
            plan.squad_cost_tenths,
            tuple(player.fpl_id for player in plan.squad.players),
        )
    )
    if not plans or not isfinite(plans[0].cumulative_xpts):
        raise ValueError("initial-squad search produced no finite completed plan")
    return InitialSquadOptimizerResult(
        recommended=plans[0],
        alternatives=tuple(plans[1:returned_squads]),
        eligible_player_ids=tuple(sorted(common_ids)),
        candidate_player_ids=candidate_ids,
        complete_squads_evaluated=len(plans),
        beam_width=beam_width,
        candidates_per_position_per_lens=candidates_per_position_per_lens,
    )
