"""Expected autosub value: bench players are neither zero-value nor guaranteed starters.

`decision/lineup.py`'s `total_xpts` only ever sums the 11 starters plus the
captain bonus -- a bench player contributes exactly zero to that score, even
though real FPL autosubs a blanking starter for the first eligible bench
player. This module computes each bench slot's OWN expected value under FPL's
actual autosub rule, as a separate, informational quantity -- it never changes
`total_xpts` or any other production score, matching the same "measurement
only" boundary every other shadow/diagnostic layer in this codebase already
observes (`docs/SPRINT4_UNCERTAINTY_AND_CALIBRATION.md`).

FPL's real autosub rule (verified via
https://www.livefpl.com/blog/fpl-auto-subs and
https://www.fantasyfootballscout.co.uk/2023/06/01/how-do-substitutes-work-in-fpl-and-what-are-autosubs):

1. A player is autosubbed only when they record EXACTLY 0 minutes for the
   whole Gameweek (a substitute appearance, however brief, is not a blank).
2. The starting goalkeeper can only be replaced by the bench goalkeeper, and
   only if the bench goalkeeper is not also on 0 minutes.
3. Outfield starters are checked against the bench IN BENCH ORDER
   (`outfield_bench_order`, left to right); the first eligible bench player
   replaces the first blanking starter found.
4. A substitution is skipped (moving to the next bench player) if it would
   leave the team without a legal formation (>=1 GK, >=3 DEF, >=1 FWD --
   reusing `decision.lineup.is_legal_starting_xi`'s own definition so this
   module can never silently diverge from it).
5. Each bench player can be used for at most one substitution.

This module computes an EXPECTED value under this rule using each player's own
`appearance_probability` (independence approximation across players, exactly
like `decision.lineup.recommend_lineup`'s own combined-uncertainty
calculation), not a simulation of one realised outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

from fpl_model.decision.lineup import (
    LineupRecommendation,
    PlayerGameweekProjection,
    is_legal_starting_xi,
)
from fpl_model.decision.squad import SquadPlayer

# Above this many blank-capable starters, exhaustively enumerating every
# blank/present combination becomes wasteful; a starter whose own blank
# probability is negligible barely moves the expected value anyway, so it is
# safe to treat as certain-to-play for this diagnostic (never for production
# scoring, which does not use this module at all).
_NEGLIGIBLE_BLANK_PROBABILITY = 0.02
_MAX_ENUMERATED_STARTERS = 12


@dataclass(frozen=True, slots=True)
class BenchSlotValue:
    fpl_id: int
    player_name: str
    expected_value: float
    """This bench slot's own expected xPts contribution via autosub -- the
    probability-weighted sum, over every blank pattern among starters, of this
    player's xPts when (and only when) they are the autosub used."""
    usage_probability: float
    """Probability this bench slot is used as an autosub in at least one
    blank pattern (may exceed the probability any single starter it could
    replace blanks, since it can substitute for more than one)."""


@dataclass(frozen=True, slots=True)
class ExpectedAutosubValue:
    bench_goalkeeper: BenchSlotValue
    outfield_bench: tuple[BenchSlotValue, ...]
    total_expected_bench_value: float
    """Sum of every bench slot's own expected_value -- the diagnostic
    counterpart to total_xpts's starters-only sum, informational only."""


def _blank_probability(projection: PlayerGameweekProjection | None) -> float:
    if projection is None or projection.appearance_probability is None:
        # No appearance evidence: treated as certain to play for this
        # diagnostic only, so a missing appearance_probability never inflates
        # expected bench value with a fabricated blank chance.
        return 0.0
    return 1.0 - projection.appearance_probability


def _resolve_substitutions(
    blanking_starter_ids: frozenset[int],
    *,
    starters: tuple[SquadPlayer, ...],
    outfield_bench_order: tuple[SquadPlayer, ...],
) -> dict[int, int]:
    """Return {bench_fpl_id: replaced_starter_fpl_id} for one blank pattern.

    Applies rule 3-5 above: bench order, first eligible replacement, skip on
    illegal formation, each bench player used at most once.
    """
    current = list(starters)
    used_bench_ids: set[int] = set()
    assignments: dict[int, int] = {}
    for starter in starters:
        if starter.fpl_id not in blanking_starter_ids:
            continue
        for bench_player in outfield_bench_order:
            if bench_player.fpl_id in used_bench_ids:
                continue
            candidate = [
                bench_player if player.fpl_id == starter.fpl_id else player for player in current
            ]
            if not is_legal_starting_xi(tuple(candidate)):
                continue
            current = candidate
            used_bench_ids.add(bench_player.fpl_id)
            assignments[bench_player.fpl_id] = starter.fpl_id
            break
    return assignments


def compute_expected_autosub_value(
    recommendation: LineupRecommendation,
    projections: dict[int, PlayerGameweekProjection],
) -> ExpectedAutosubValue:
    """Compute each bench slot's expected xPts contribution under the real autosub rule.

    Enumerates every combination of which starters blank (each independently,
    using their own ``appearance_probability``), resolves autosubs for each
    combination via the same bench-order/formation-legality rule FPL applies,
    and weights each bench player's xPts by the probability of the specific
    blank pattern that uses them. Starters whose own blank probability is
    below a negligible threshold are treated as certain to play, keeping the
    enumeration tractable without materially changing the result -- this is a
    diagnostic approximation, never applied to `total_xpts`.
    """
    starters = recommendation.starters
    blank_probabilities = {
        player.fpl_id: _blank_probability(projections.get(player.fpl_id)) for player in starters
    }
    relevant_starters = tuple(
        player
        for player in starters
        if blank_probabilities[player.fpl_id] > _NEGLIGIBLE_BLANK_PROBABILITY
    )
    if len(relevant_starters) > _MAX_ENUMERATED_STARTERS:
        # Keep only the highest blank-probability starters; the rest are
        # treated as certain to play for this enumeration, which understates
        # expected bench value only in the vanishingly rare case of many
        # simultaneous high-uncertainty blanks.
        relevant_starters = tuple(
            sorted(
                relevant_starters,
                key=lambda player: -blank_probabilities[player.fpl_id],
            )[:_MAX_ENUMERATED_STARTERS]
        )
    relevant_ids = tuple(player.fpl_id for player in relevant_starters)

    bench_expected: dict[int, float] = {
        player.fpl_id: 0.0
        for player in (recommendation.bench_goalkeeper, *recommendation.outfield_bench_order)
    }
    bench_usage: dict[int, float] = dict.fromkeys(bench_expected, 0.0)

    # Goalkeeper autosub is independent of outfield substitutions (rule 2).
    gk = next(player for player in starters if player.position == "GK")
    gk_blank = blank_probabilities[gk.fpl_id]
    bench_gk_projection = projections.get(recommendation.bench_goalkeeper.fpl_id)
    bench_gk_blank = _blank_probability(bench_gk_projection)
    gk_sub_probability = gk_blank * (1.0 - bench_gk_blank)
    if bench_gk_projection is not None:
        bench_expected[recommendation.bench_goalkeeper.fpl_id] += (
            gk_sub_probability * bench_gk_projection.expected_points
        )
    bench_usage[recommendation.bench_goalkeeper.fpl_id] += gk_sub_probability

    for pattern in product([False, True], repeat=len(relevant_ids)):
        pattern_probability = 1.0
        blanking: set[int] = set()
        for fpl_id, blanks in zip(relevant_ids, pattern, strict=True):
            probability = blank_probabilities[fpl_id]
            pattern_probability *= probability if blanks else (1.0 - probability)
            if blanks:
                blanking.add(fpl_id)
        if pattern_probability <= 0.0 or not blanking:
            continue
        assignments = _resolve_substitutions(
            frozenset(blanking),
            starters=starters,
            outfield_bench_order=recommendation.outfield_bench_order,
        )
        for bench_fpl_id in assignments:
            projection = projections.get(bench_fpl_id)
            if projection is None:
                continue
            bench_expected[bench_fpl_id] += pattern_probability * projection.expected_points
            bench_usage[bench_fpl_id] += pattern_probability

    def _slot(player: SquadPlayer) -> BenchSlotValue:
        return BenchSlotValue(
            fpl_id=player.fpl_id,
            player_name=player.player_name,
            expected_value=bench_expected[player.fpl_id],
            usage_probability=min(1.0, bench_usage[player.fpl_id]),
        )

    bench_goalkeeper_value = _slot(recommendation.bench_goalkeeper)
    outfield_values = tuple(_slot(player) for player in recommendation.outfield_bench_order)
    return ExpectedAutosubValue(
        bench_goalkeeper=bench_goalkeeper_value,
        outfield_bench=outfield_values,
        total_expected_bench_value=bench_goalkeeper_value.expected_value
        + sum(row.expected_value for row in outfield_values),
    )
