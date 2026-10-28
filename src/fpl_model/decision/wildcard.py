"""Five-GW Wildcard versus hold/FT scenarios, using actual liquidation value."""

from dataclasses import dataclass, replace

from fpl_model.decision.initial_squad import SquadConstraints, optimize_initial_squad
from fpl_model.decision.lineup import LineupRecommendation, recommend_lineup
from fpl_model.decision.rolling import GameweekProjectionPool, RollingPlan, plan_rolling_horizon
from fpl_model.decision.squad import ValidatedSquad, validate_squad


@dataclass(frozen=True, slots=True)
class WildcardComparison:
    hold: RollingPlan
    transfers: RollingPlan
    wildcard_squad: ValidatedSquad
    wildcard: RollingPlan
    wildcard_first_lineup: LineupRecommendation
    wildcard_total_xpts: float
    wildcard_objective: float
    wildcard_gain_vs_hold: float
    wildcard_gain_vs_transfers: float
    retained_free_transfers: int
    search_is_exact: bool = False


def wildcard_candidates(squad, pools, *, constraints=None, beam_width=120, returned_squads=3):
    """Owned players cost their sale value to retain; restore real prices afterwards."""
    owned = {p.fpl_id: p for p in squad.players}
    priced = tuple(replace(pool, players=tuple(
        replace(target, player=replace(target.player, current_price_tenths=owned[target.player.fpl_id].selling_price_tenths))
        if target.player.fpl_id in owned else target for target in pool.players
    )) for pool in pools)
    result = optimize_initial_squad(
        priced, budget_tenths=squad.team_value_tenths, constraints=constraints,
        beam_width=beam_width, candidates_per_position_per_lens=6, returned_squads=returned_squads,
    )
    market = {t.player.fpl_id: t.player for t in pools[0].players}
    candidates = []
    for plan in (result.recommended, *result.alternatives):
        players = []
        for p in plan.squad.players:
            source = owned.get(p.fpl_id, market[p.fpl_id])
            players.append(replace(
                source, squad_position=p.squad_position, is_captain=p.is_captain,
                is_vice_captain=p.is_vice_captain,
                purchase_price_tenths=source.purchase_price_tenths if p.fpl_id in owned else source.current_price_tenths,
                selling_price_tenths=source.selling_price_tenths if p.fpl_id in owned else source.current_price_tenths,
            ))
        chips = dict(squad.chip_states)
        chips['wildcard'] = 'played'
        candidates.append(validate_squad(
            players, bank_tenths=plan.bank_tenths, free_transfers=squad.free_transfers,
            unlimited_transfers=False, chip_period=squad.chip_period, chip_states=chips,
        ))
    constraints = constraints or SquadConstraints()
    if constraints.locked_fpl_ids <= owned.keys() and not constraints.excluded_fpl_ids & owned.keys():
        chips = dict(squad.chip_states)
        chips['wildcard'] = 'played'
        try:
            incumbent = validate_squad(squad.players, bank_tenths=squad.bank_tenths,
                                       free_transfers=squad.free_transfers, unlimited_transfers=False,
                                       chip_period=squad.chip_period, chip_states=chips)
            if not any({p.fpl_id for p in c.players} == owned.keys() for c in candidates):
                candidates.append(incumbent)
        except ValueError:
            pass  # A grandfathered club-limit squad cannot be retained on WC.
    return tuple(candidates)


def compare_wildcard(
    squad: ValidatedSquad, pools: tuple[GameweekProjectionPool, ...], *,
    roll_after_wildcard: int = 3, terminal_ft_value: float = 0.0,
    constraints: SquadConstraints | None = None, beam_width: int = 120,
    transfer_beam_width: int = 6,
) -> WildcardComparison:
    if not 2 <= len(pools) <= 5:
        raise ValueError('Wildcard comparison requires two to five published Gameweeks')
    if not 0 <= roll_after_wildcard < len(pools):
        raise ValueError('post-Wildcard roll period must fit the published horizon')
    if dict(squad.chip_states).get('wildcard') != 'available':
        raise ValueError('Wildcard must be available')
    if squad.free_transfers is None:
        raise ValueError('saved free transfers must be explicit')
    options = dict(terminal_ft_value=terminal_ft_value, beam_width=transfer_beam_width,
                   candidates_per_position=3, max_transfers_per_gameweek=2, bundle_shortlist=8)
    hold = plan_rolling_horizon(squad, pools, forced_roll_gameweeks=frozenset(p.gameweek for p in pools), **options).recommended
    transfers = plan_rolling_horizon(squad, pools, **options).recommended
    # Always retain hold as a valid counterfactual even if beam pruning loses it.
    if hold.objective_score > transfers.objective_score:
        transfers = hold
    candidates = wildcard_candidates(squad, pools, constraints=constraints, beam_width=beam_width)
    scored = []
    for candidate in candidates:
        first = recommend_lineup(candidate, tuple(t.projection for t in pools[0].players
                                                  if t.player.fpl_id in {p.fpl_id for p in candidate.players}))
        # WC retains the banked FT count and grants no extra FT at its deadline.
        tail = plan_rolling_horizon(
            candidate, pools[1:],
            forced_roll_gameweeks=frozenset(p.gameweek for p in pools[1:1 + roll_after_wildcard]),
            **options,
        ).recommended
        total = first.total_xpts + tail.cumulative_net_xpts
        scored.append((total + tail.terminal_ft_value, total, candidate, first, tail))
    objective, total, selected, first, tail = max(scored, key=lambda row: row[0])
    return WildcardComparison(
        hold=hold, transfers=transfers, wildcard_squad=selected, wildcard=tail,
        wildcard_first_lineup=first, wildcard_total_xpts=total, wildcard_objective=objective,
        wildcard_gain_vs_hold=total - hold.cumulative_net_xpts,
        wildcard_gain_vs_transfers=total - transfers.cumulative_net_xpts,
        retained_free_transfers=int(squad.free_transfers),
    )
