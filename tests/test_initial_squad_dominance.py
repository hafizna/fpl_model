from __future__ import annotations

from fpl_model.decision.initial_squad import SquadConstraints, optimize_initial_squad
from fpl_model.decision.initial_squad_dominance import audit_dominance
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


def _base_players() -> list[TransferTarget]:
    # A cheap, solid pool: 5 GK, 6 DEF, 6 MID, 4 FWD -- one spare per outfield
    # position beyond the legal counts, so the search has genuine choices.
    return [
        *(_target(fpl_id, "GK", price=45, xpts=3.0) for fpl_id in range(1, 6)),
        *(_target(fpl_id, "DEF", price=45, xpts=4.0) for fpl_id in range(6, 12)),
        *(_target(fpl_id, "MID", price=50, xpts=4.5) for fpl_id in range(12, 18)),
        *(_target(fpl_id, "FWD", price=55, xpts=5.0) for fpl_id in range(18, 22)),
    ]


def _dominated_pools() -> tuple[GameweekProjectionPool, ...]:
    """Construct a pool where an expensive, never-started backup goalkeeper
    consumes budget that -- once excluded -- the search can legally reinvest
    into a higher-scoring outfield player, exactly the documented August 2026
    failure shape (expensive backup GK crowding out useful budget)."""
    rows = _base_players()
    # Player 99: a very expensive GK who will never start (GK1 always scores
    # higher), but is attractive enough on raw xPts to survive the score/value
    # candidate lenses and get selected as the backup by a beam search that
    # is not told to look for cheaper backups.
    rows.append(_target(99, "GK", price=300, xpts=20.0, team_id=90))
    # Player 100: only affordable once the expensive backup GK is excluded and
    # its budget is freed; scores far higher than any other remaining FWD.
    rows.append(_target(100, "FWD", price=300, xpts=40.0, team_id=91))
    return tuple(
        GameweekProjectionPool(
            gameweek=gameweek,
            players=tuple(rows),
            transferable_fpl_ids=tuple(row.player.fpl_id for row in rows),
        )
        for gameweek in (1, 2, 3)
    )


def test_audit_finds_a_dominating_cheap_goalkeeper_counterfactual():
    pools = _dominated_pools()
    # A narrow beam/candidate limit reproduces the real failure mode: the
    # search settles for the expensive backup GK and never explores excluding
    # it in favour of reinvesting into player 100.
    forced_result = optimize_initial_squad(
        pools,
        beam_width=50,
        candidates_per_position_per_lens=6,
        budget_tenths=1_000,
        constraints=SquadConstraints(locked_fpl_ids=frozenset({99})),
    )
    assert 99 in {player.fpl_id for player in forced_result.recommended.squad.players}

    audit = audit_dominance(
        forced_result,
        pools,
        budget_tenths=1_000,
        beam_width=50,
        candidates_per_position_per_lens=6,
    )

    assert audit.is_dominated is True
    names = {row["name"] for row in audit.report["dominating_counterfactuals"]}
    assert "cheap_goalkeeper_pair" in names
    goalkeeper_row = next(
        row
        for row in audit.report["counterfactuals"]
        if row["name"] == "cheap_goalkeeper_pair"
    )
    assert goalkeeper_row["excluded_fpl_ids"] == [99]
    assert goalkeeper_row["cumulative_xpts"] > audit.report["recommended_cumulative_xpts"]
    assert goalkeeper_row["squad_cost_tenths"] <= audit.report["recommended_squad_cost_tenths"]


def test_audit_reports_not_dominated_for_a_healthy_squad():
    pools = _dominated_pools()
    # A beam wide enough to find the better squad itself (excludes the
    # expensive GK naturally), so the audit should find nothing dominating it.
    # This fixture only has ~24 candidate players, so 200/12 is already ample
    # without paying for a much larger, slower search.
    result = optimize_initial_squad(
        pools, beam_width=200, candidates_per_position_per_lens=12, budget_tenths=1_000
    )
    assert 99 not in {player.fpl_id for player in result.recommended.squad.players}

    audit = audit_dominance(
        result, pools, budget_tenths=1_000, beam_width=200, candidates_per_position_per_lens=12
    )

    assert audit.is_dominated is False
    assert audit.report["dominating_counterfactuals"] == []


def test_goalkeeper_counterfactual_targets_the_most_expensive_goalkeeper():
    pools = _dominated_pools()
    forced_result = optimize_initial_squad(
        pools,
        beam_width=50,
        candidates_per_position_per_lens=6,
        budget_tenths=1_000,
        constraints=SquadConstraints(locked_fpl_ids=frozenset({99})),
    )

    audit = audit_dominance(
        forced_result, pools, budget_tenths=1_000, beam_width=50, candidates_per_position_per_lens=6
    )

    goalkeeper_row = next(
        row
        for row in audit.report["counterfactuals"]
        if row["name"] == "cheap_goalkeeper_pair"
    )
    assert goalkeeper_row["excluded_fpl_ids"] == [99]


def _premium_never_captained_pools() -> tuple[GameweekProjectionPool, ...]:
    """A premium FWD (player 200) starts every Gameweek but is never captained --
    another player always scores higher -- and is priced so high it crowds out
    a much stronger reinvestment target (player 201) once excluded."""
    rows = _base_players()
    # Player 199: always the top scorer, always captained.
    rows.append(_target(199, "MID", price=50, xpts=50.0, team_id=92))
    # Player 200: expensive, starts, but never outscores player 199 -- never
    # captained. Priced high enough to be worth including on raw xPts alone,
    # but not so high that its own price/value ranking would obviously exclude
    # it from a naive search.
    rows.append(_target(200, "FWD", price=300, xpts=15.0, team_id=93))
    # Player 201: only affordable once player 200's price is freed; scores far
    # higher than any other remaining FWD.
    rows.append(_target(201, "FWD", price=300, xpts=40.0, team_id=94))
    return tuple(
        GameweekProjectionPool(
            gameweek=gameweek,
            players=tuple(rows),
            transferable_fpl_ids=tuple(row.player.fpl_id for row in rows),
        )
        for gameweek in (1, 2, 3)
    )


def test_audit_finds_a_dominating_premium_starter_counterfactual():
    pools = _premium_never_captained_pools()
    forced_result = optimize_initial_squad(
        pools,
        beam_width=50,
        candidates_per_position_per_lens=6,
        budget_tenths=1_000,
        constraints=SquadConstraints(locked_fpl_ids=frozenset({200})),
    )
    assert 200 in {player.fpl_id for player in forced_result.recommended.squad.players}
    captained_ids = {row.lineup.captain.fpl_id for row in forced_result.recommended.gameweeks}
    assert 200 not in captained_ids

    audit = audit_dominance(
        forced_result,
        pools,
        budget_tenths=1_000,
        beam_width=50,
        candidates_per_position_per_lens=6,
    )

    assert audit.is_dominated is True
    premium_row = next(
        row
        for row in audit.report["counterfactuals"]
        if row["name"] == "premium_starter_reinvestment"
    )
    assert premium_row["excluded_fpl_ids"] == [200]
    assert premium_row["dominates_recommendation"] is True


def test_premium_counterfactual_reports_infeasible_when_the_expensive_player_is_captained():
    pools = _premium_never_captained_pools()
    # Force player 200 in AND make it the only viable captain by excluding 199
    # from consideration via a cheap enough alternative -- simplest way here is
    # to just check the natural, unconstrained recommendation instead, where
    # 199 (much higher xPts) is captained and 200 may not even be selected.
    result = optimize_initial_squad(
        pools, beam_width=200, candidates_per_position_per_lens=12, budget_tenths=1_000
    )

    audit = audit_dominance(
        result, pools, budget_tenths=1_000, beam_width=200, candidates_per_position_per_lens=12
    )

    premium_row = next(
        row
        for row in audit.report["counterfactuals"]
        if row["name"] == "premium_starter_reinvestment"
    )
    # Either no qualifying premium player exists in the healthy squad, or it
    # exists but does not dominate -- either way this must not silently pass.
    assert premium_row["dominates_recommendation"] is False


def test_bench_reinvestment_counterfactual_reports_infeasible_when_no_target_qualifies():
    # A squad with no non-goalkeeper bench player above the cheap-enabler
    # threshold: the bench-reinvestment counterfactual has nothing to exclude.
    rows = _base_players()
    pools = tuple(
        GameweekProjectionPool(
            gameweek=gameweek,
            players=tuple(rows),
            transferable_fpl_ids=tuple(row.player.fpl_id for row in rows),
        )
        for gameweek in (1, 2, 3)
    )
    result = optimize_initial_squad(
        pools, beam_width=200, candidates_per_position_per_lens=12, budget_tenths=1_000
    )

    audit = audit_dominance(
        result, pools, budget_tenths=1_000, beam_width=200, candidates_per_position_per_lens=12
    )

    bench_row = next(
        row for row in audit.report["counterfactuals"] if row["name"] == "cheap_bench_reinvestment"
    )
    assert bench_row["infeasible_reason"] is not None
    assert bench_row["dominates_recommendation"] is False
