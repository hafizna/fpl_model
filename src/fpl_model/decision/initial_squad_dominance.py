"""Audit an initial-squad beam-search recommendation against structural counterfactuals.

`decision/initial_squad.py`'s beam search is approximate: candidate pruning can
miss a legal, cheaper-or-equal, higher-xPts squad entirely -- exactly the
documented August 2026 diagnostic failure in `README.md` (a 188.68 xPts squad
with two GBP 5.0m goalkeepers and a benched GBP 8.0m Watkins, dominated by a
manually locked Raya/Dubravka structure scoring 189.09 xPts at the same
budget). Rather than trying to make the beam search itself exhaustive, this
module runs a small number of NAMED, targeted counterfactual searches -- using
the same `SquadConstraints` mechanism a manager could use manually -- and
checks whether any of them dominates the beam's own recommendation.

A counterfactual squad C dominates a squad R when C's cumulative three-Gameweek
xPts is at least R's AND C's squad cost is at most R's, with at least one strict
inequality (the standard Pareto-dominance definition on this two-objective
problem). This module never changes what the beam search returns; it only
labels the result and surfaces the beating alternative when one exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fpl_model.decision.initial_squad import (
    DEFAULT_CANDIDATES_PER_POSITION_PER_LENS,
    DEFAULT_INITIAL_SQUAD_BEAM_WIDTH,
    InitialSquadOptimizerResult,
    InitialSquadPlan,
    SquadConstraints,
    optimize_initial_squad,
)
from fpl_model.decision.rolling import GameweekProjectionPool

# Reuses validation.projection_coverage.CHEAP_ENABLER_MAX_PRICE's own
# position/price convention for "cheap enabler", so a counterfactual asks
# exactly the same "reinvest into a cheap structure" question that coverage
# audit's cheap-enabler cohort already names.
CHEAP_ENABLER_MAX_PRICE_TENTHS = {"GK": 45, "DEF": 45, "MID": 55, "FWD": 55}

# A "premium" player costs strictly more than the cheap-enabler ceiling for
# their own position by at least this many tenths -- distinguishing a genuine
# premium pick from an ordinary mid-priced player who simply is not always
# captained.
PREMIUM_PRICE_MARGIN_TENTHS = 20


@dataclass(frozen=True, slots=True)
class CounterfactualResult:
    name: str
    description: str
    excluded_fpl_ids: tuple[int, ...]
    plan: InitialSquadPlan | None
    infeasible_reason: str | None


@dataclass(frozen=True, slots=True)
class DominanceAudit:
    report: dict[str, Any]

    @property
    def is_dominated(self) -> bool:
        return bool(self.report["is_dominated"])


def _dominates(challenger: InitialSquadPlan, incumbent: InitialSquadPlan) -> bool:
    xpts_at_least = challenger.cumulative_xpts >= incumbent.cumulative_xpts
    cost_at_most = challenger.squad_cost_tenths <= incumbent.squad_cost_tenths
    strictly_better = (
        challenger.cumulative_xpts > incumbent.cumulative_xpts
        or challenger.squad_cost_tenths < incumbent.squad_cost_tenths
    )
    return xpts_at_least and cost_at_most and strictly_better


def _expensive_bench_players(
    recommended: InitialSquadPlan,
) -> dict[int, tuple[str, str, int]]:
    """fpl_id -> (position, player_name, price_tenths) for every player benched
    in EVERY Gameweek who is priced above the cheap-enabler threshold for their
    position -- a player the squad is paying a premium for but never starting."""
    bench_gameweeks: dict[int, int] = {}
    identity: dict[int, tuple[str, str, int]] = {}
    for row in recommended.gameweeks:
        for player in (row.lineup.bench_goalkeeper, *row.lineup.outfield_bench_order):
            bench_gameweeks[player.fpl_id] = bench_gameweeks.get(player.fpl_id, 0) + 1
            identity[player.fpl_id] = (
                player.position,
                player.player_name,
                player.current_price_tenths,
            )

    total_gameweeks = len(recommended.gameweeks)
    expensive = {}
    for fpl_id, count in bench_gameweeks.items():
        if count != total_gameweeks:
            continue
        position, name, price = identity[fpl_id]
        threshold = CHEAP_ENABLER_MAX_PRICE_TENTHS.get(position)
        if threshold is not None and price > threshold:
            expensive[fpl_id] = (position, name, price)
    return expensive


def _most_expensive_never_captained_premium(
    recommended: InitialSquadPlan,
) -> tuple[int, str, int] | None:
    """The single most expensive player, above the premium price margin for
    their position, who is never captained across the retained horizon --
    a manager paying a premium price without the squad ever backing them with
    the captaincy's double points, the specific "rarely started or captained"
    shape design principle 6 names."""
    captained_ids = {row.lineup.captain.fpl_id for row in recommended.gameweeks}
    candidates: dict[int, tuple[str, str, int]] = {}
    for player in recommended.squad.players:
        if player.fpl_id in captained_ids:
            continue
        threshold = CHEAP_ENABLER_MAX_PRICE_TENTHS.get(player.position)
        if threshold is None or player.current_price_tenths < threshold + PREMIUM_PRICE_MARGIN_TENTHS:
            continue
        candidates[player.fpl_id] = (
            player.position,
            player.player_name,
            player.current_price_tenths,
        )
    if not candidates:
        return None
    fpl_id, (_, name, price) = max(candidates.items(), key=lambda item: (item[1][2], item[0]))
    return fpl_id, name, price


def _run_counterfactual(
    *,
    name: str,
    description: str,
    excluded_fpl_ids: tuple[int, ...],
    pools: tuple[GameweekProjectionPool, ...],
    budget_tenths: int,
    beam_width: int,
    candidates_per_position_per_lens: int,
    plan_future_transfers: bool,
    planned_transfer_shortlist: int,
) -> CounterfactualResult:
    if not excluded_fpl_ids:
        return CounterfactualResult(
            name=name,
            description=description,
            excluded_fpl_ids=excluded_fpl_ids,
            plan=None,
            infeasible_reason="no target player identified for this counterfactual",
        )
    try:
        result = optimize_initial_squad(
            pools,
            budget_tenths=budget_tenths,
            beam_width=beam_width,
            candidates_per_position_per_lens=candidates_per_position_per_lens,
            returned_squads=1,
            constraints=SquadConstraints(excluded_fpl_ids=frozenset(excluded_fpl_ids)),
            plan_future_transfers=plan_future_transfers,
            planned_transfer_shortlist=planned_transfer_shortlist,
        )
    except ValueError as error:
        return CounterfactualResult(
            name=name,
            description=description,
            excluded_fpl_ids=excluded_fpl_ids,
            plan=None,
            infeasible_reason=str(error),
        )
    return CounterfactualResult(
        name=name,
        description=description,
        excluded_fpl_ids=excluded_fpl_ids,
        plan=result.recommended,
        infeasible_reason=None,
    )


def audit_dominance(
    result: InitialSquadOptimizerResult,
    pools: tuple[GameweekProjectionPool, ...],
    *,
    budget_tenths: int,
    beam_width: int = DEFAULT_INITIAL_SQUAD_BEAM_WIDTH,
    candidates_per_position_per_lens: int = DEFAULT_CANDIDATES_PER_POSITION_PER_LENS,
    plan_future_transfers: bool = False,
    planned_transfer_shortlist: int = 30,
) -> DominanceAudit:
    """Check the beam search's own recommendation against named counterfactuals.

    Runs three structural counterfactuals via the search's own ``SquadConstraints``
    mechanism, matching design principle 6's required comparisons:

    - ``cheap_goalkeeper_pair``: exclude the recommended squad's most expensive
      goalkeeper (starter or bench) and let the search reinvest freely -- tests
      whether a cheaper goalkeeper structure beats the expensive one.
    - ``cheap_bench_reinvestment``: exclude every non-goalkeeper bench player
      priced above the cheap-enabler threshold who was benched in every
      Gameweek -- tests whether reinvesting an expensive, never-started bench
      spot beats keeping it.
    - ``premium_starter_reinvestment``: exclude the single most expensive
      premium-priced player who is never captained across the retained
      horizon -- tests "premium sanity": a player commanding a premium price
      must earn that price through marginal horizon value, not merely a
      squad slot.

    Each counterfactual re-runs the full beam search (same beam width/candidate
    limit as the original) with that player excluded; the search is free to
    reallocate the freed budget however it likes. `is_dominated` is true when
    any counterfactual's own recommended squad Pareto-dominates the original.
    This never changes what `optimize_initial_squad` returns -- it is read-only
    auditing layered on top.
    """
    recommended = result.recommended
    counterfactuals: list[CounterfactualResult] = []

    goalkeepers = [
        (player.fpl_id, player.player_name, player.current_price_tenths)
        for player in recommended.squad.players
        if player.position == "GK"
    ]
    most_expensive_gk = max(goalkeepers, key=lambda row: (row[2], row[0]), default=None)
    counterfactuals.append(
        _run_counterfactual(
            name="cheap_goalkeeper_pair",
            description=(
                "Exclude the recommended squad's most expensive goalkeeper and let the "
                "search reinvest freely -- tests a cheaper goalkeeper structure."
            ),
            excluded_fpl_ids=() if most_expensive_gk is None else (most_expensive_gk[0],),
            pools=pools,
            budget_tenths=budget_tenths,
            beam_width=beam_width,
            candidates_per_position_per_lens=candidates_per_position_per_lens,
            plan_future_transfers=plan_future_transfers,
            planned_transfer_shortlist=planned_transfer_shortlist,
        )
    )

    expensive_bench = _expensive_bench_players(recommended)
    expensive_bench_non_gk = tuple(
        fpl_id for fpl_id, (position, _, _) in expensive_bench.items() if position != "GK"
    )
    counterfactuals.append(
        _run_counterfactual(
            name="cheap_bench_reinvestment",
            description=(
                "Exclude every non-goalkeeper bench player priced above the cheap-enabler "
                "threshold who was benched in every Gameweek -- tests reinvesting an "
                "expensive, never-started bench spot."
            ),
            excluded_fpl_ids=expensive_bench_non_gk,
            pools=pools,
            budget_tenths=budget_tenths,
            beam_width=beam_width,
            candidates_per_position_per_lens=candidates_per_position_per_lens,
            plan_future_transfers=plan_future_transfers,
            planned_transfer_shortlist=planned_transfer_shortlist,
        )
    )

    premium = _most_expensive_never_captained_premium(recommended)
    counterfactuals.append(
        _run_counterfactual(
            name="premium_starter_reinvestment",
            description=(
                "Exclude the single most expensive premium-priced player never captained "
                "across the retained horizon -- tests whether that premium price is earning "
                "its marginal horizon value."
            ),
            excluded_fpl_ids=() if premium is None else (premium[0],),
            pools=pools,
            budget_tenths=budget_tenths,
            beam_width=beam_width,
            candidates_per_position_per_lens=candidates_per_position_per_lens,
            plan_future_transfers=plan_future_transfers,
            planned_transfer_shortlist=planned_transfer_shortlist,
        )
    )

    dominating: list[dict[str, Any]] = []
    counterfactual_reports = []
    for counterfactual in counterfactuals:
        dominates = (
            counterfactual.plan is not None and _dominates(counterfactual.plan, recommended)
        )
        counterfactual_reports.append(
            {
                "name": counterfactual.name,
                "description": counterfactual.description,
                "excluded_fpl_ids": list(counterfactual.excluded_fpl_ids),
                "infeasible_reason": counterfactual.infeasible_reason,
                "cumulative_xpts": (
                    None if counterfactual.plan is None else counterfactual.plan.cumulative_xpts
                ),
                "squad_cost_tenths": (
                    None if counterfactual.plan is None else counterfactual.plan.squad_cost_tenths
                ),
                "dominates_recommendation": dominates,
            }
        )
        if dominates:
            dominating.append(counterfactual_reports[-1])

    payload: dict[str, Any] = {
        "label": "initial_squad_dominance_audit_v1",
        "recommended_cumulative_xpts": recommended.cumulative_xpts,
        "recommended_squad_cost_tenths": recommended.squad_cost_tenths,
        "is_dominated": bool(dominating),
        "dominating_counterfactuals": dominating,
        "counterfactuals": counterfactual_reports,
        "limitations": [
            "This audits three named structural counterfactuals (goalkeeper pair, expensive "
            "bench reinvestment, premium starter reinvestment) matching design principle 6, "
            "not an exhaustive proof of global optimality. A dominating alternative not "
            "covered by these three named counterfactuals would not be found.",
            "Each counterfactual reruns the same approximate beam search with a player "
            "excluded; its own recommendation is therefore also not a certified optimum.",
            "'infeasible_reason' set (rather than a plan) means that counterfactual could not "
            "be evaluated -- e.g. no qualifying player was found, or excluding it left no "
            "legal squad -- and does not itself indicate dominance either way.",
        ],
    }
    return DominanceAudit(report=payload)
