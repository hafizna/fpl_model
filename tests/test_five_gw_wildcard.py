from dataclasses import replace
from itertools import combinations
from random import Random

import pytest

from fpl_model.decision.initial_squad import SquadConstraints
from fpl_model.decision.lineup import is_legal_starting_xi, recommend_lineup
from fpl_model.decision.rolling import plan_rolling_horizon
from fpl_model.decision.transfer_bundle import apply_transfer_bundle
from fpl_model.decision.wildcard import compare_wildcard, wildcard_candidates
from tests.test_rolling import _pool
from tests.test_squad import _players, _validate
from tests.test_transfer import _target


def test_five_gameweek_hold_caps_saved_ft_and_prices_its_assumed_value():
    squad = _validate(_players(), free_transfers=2)
    pools = tuple(_pool(gw, candidate_xpts=100) for gw in range(4, 9))
    plan = plan_rolling_horizon(squad, pools, forced_roll_gameweeks=frozenset(range(4, 9)), terminal_ft_value=1.5).recommended
    assert [s.free_transfers_after for s in plan.steps] == [3, 4, 5, 5, 5]
    assert plan.terminal_ft_value == 7.5
    assert plan.objective_score == plan.cumulative_net_xpts + 7.5


def test_bundle_can_fund_upgrade_and_does_not_require_intermediate_club_legality():
    squad = _validate(_players(), bank_tenths=0)
    # Buying the FWD first is unaffordable. Buying the MID first temporarily
    # creates four players from club 1. The final two-move squad is legal.
    forward = _target(price=85, team_id=9).player
    midfielder = replace(forward, fpl_id=17, position='MID', current_price_tenths=20, team_id=1)
    result = apply_transfer_bundle(squad, ((11, forward), (5, midfielder)))
    assert result.bank_tenths == 61 + 55 - 85 - 20
    assert {p.fpl_id for p in result.players} == ({p.fpl_id for p in squad.players} - {11, 5}) | {16, 17}
    with pytest.raises(ValueError):
        apply_transfer_bundle(squad, ((11, forward),))
    with pytest.raises(ValueError):
        apply_transfer_bundle(squad, ((5, midfielder),))


def test_planner_finds_jointly_funded_upgrade_and_counts_two_fts():
    squad = _validate(_players(), bank_tenths=0)
    pool = _pool(4, candidate_xpts=None)
    forward = _target(price=85, xpts=40, team_id=9)
    cheap = replace(forward, player=replace(forward.player, fpl_id=17, position='MID', current_price_tenths=20),
                    projection=replace(forward.projection, fpl_id=17, expected_points=0))
    pool = replace(pool, players=(*pool.players, forward, cheap))
    plan = plan_rolling_horizon(squad, (pool,), max_transfers_per_gameweek=2).recommended
    assert len(plan.steps[0].transfers) == 2
    assert plan.steps[0].free_transfers_before == 2
    assert plan.steps[0].free_transfers_after == 1
    assert plan.total_transfer_cost == 0


def test_wildcard_retains_ft_and_rolls_three_weeks_using_actual_sale_values():
    players = _players()
    players[0] = replace(players[0], purchase_price_tenths=47, selling_price_tenths=49)
    squad = _validate(players, bank_tenths=0, free_transfers=2)
    pools = tuple(_pool(gw, candidate_xpts=None) for gw in range(4, 9))
    locks = SquadConstraints(locked_fpl_ids=frozenset(p.fpl_id for p in players))
    candidate = wildcard_candidates(squad, pools, constraints=locks, beam_width=10)[0]
    retained = next(p for p in candidate.players if p.fpl_id == 1)
    assert retained.current_price_tenths == 51
    assert retained.purchase_price_tenths == 47
    assert retained.selling_price_tenths == 49
    assert candidate.bank_tenths == 0
    result = compare_wildcard(squad, pools, constraints=locks, beam_width=10)
    assert result.retained_free_transfers == 2
    assert [s.free_transfers_before for s in result.wildcard.steps] == [2, 3, 4, 5]
    assert [s.free_transfers_after for s in result.wildcard.steps] == [3, 4, 5, 5]
    assert result.wildcard_total_xpts == pytest.approx(result.hold.cumulative_net_xpts)


def test_formation_search_matches_exhaustive_subsets_including_ties_and_negative_points():
    random = Random(2026)
    squad = _validate(_players())
    base = _pool(4, candidate_xpts=None)
    for _ in range(25):
        projections = tuple(replace(t.projection, expected_points=float(random.randrange(-3, 10))) for t in base.players)
        points = {p.fpl_id: p.expected_points for p in projections}
        expected = max((candidate for candidate in combinations(squad.players, 11) if is_legal_starting_xi(candidate)),
                       key=lambda candidate: sum(points[p.fpl_id] for p in candidate))
        actual = recommend_lineup(squad, projections)
        assert actual.starters == expected
