"""Exhaustive, explainable single-Gameweek lineup recommendation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations
from math import isfinite, sqrt

from fpl_model.decision.squad import SquadPlayer, ValidatedSquad


@dataclass(frozen=True, slots=True)
class PlayerGameweekProjection:
    fpl_id: int
    expected_points: float
    uncertainty: float | None = None
    data_quality_flags: tuple[str, ...] = ()
    appearance_probability: float | None = None
    """Probability of playing >=1 minute this Gameweek (start or substitute
    appearance, combined across every fixture for a double Gameweek). ``None``
    when the upstream appearance projection is unavailable. This is the
    quantity FPL's own autosub rule keys off (a player is autosubbed only on
    exactly 0 minutes for the whole Gameweek) -- see `decision/autosub.py`.
    """

    def __post_init__(self) -> None:
        if self.fpl_id <= 0:
            raise ValueError("fpl_id must be positive")
        if not isfinite(self.expected_points):
            raise ValueError("expected_points must be finite")
        if self.uncertainty is not None and (
            not isfinite(self.uncertainty) or self.uncertainty < 0.0
        ):
            raise ValueError("uncertainty must be finite and non-negative")
        if self.appearance_probability is not None and not (
            0.0 <= self.appearance_probability <= 1.0
        ):
            raise ValueError("appearance_probability must be between 0.0 and 1.0")


@dataclass(frozen=True, slots=True)
class LineupRecommendation:
    starters: tuple[SquadPlayer, ...]
    bench_goalkeeper: SquadPlayer
    outfield_bench_order: tuple[SquadPlayer, ...]
    captain: SquadPlayer
    vice_captain: SquadPlayer
    starting_xpts: float
    captain_bonus_xpts: float
    total_xpts: float
    uncertainty: float | None
    data_quality_flags: tuple[str, ...]

    @property
    def formation(self) -> str:
        counts = {
            position: sum(player.position == position for player in self.starters)
            for position in ("DEF", "MID", "FWD")
        }
        return f"{counts['DEF']}-{counts['MID']}-{counts['FWD']}"


def is_legal_starting_xi(players: tuple[SquadPlayer, ...]) -> bool:
    return (
        sum(player.position == "GK" for player in players) == 1
        and sum(player.position == "DEF" for player in players) >= 3
        and sum(player.position == "FWD" for player in players) >= 1
    )


def recommend_lineup(
    squad: ValidatedSquad,
    projections: Iterable[PlayerGameweekProjection],
) -> LineupRecommendation:
    """Return the maximum-mean-xPts legal XI and deterministic bench/captain order.

    All 1,365 possible 11-player subsets are cheap to enumerate. This is
    preferable to introducing a solver before transfer and chip decisions
    create a genuinely larger search space.
    """
    projection_rows = tuple(projections)
    projection_by_id = {projection.fpl_id: projection for projection in projection_rows}
    if len(projection_by_id) != len(projection_rows):
        raise ValueError("projections contain duplicate fpl_id values")

    squad_ids = {player.fpl_id for player in squad.players}
    projection_ids = set(projection_by_id)
    missing = sorted(squad_ids - projection_ids)
    unexpected = sorted(projection_ids - squad_ids)
    if missing or unexpected:
        raise ValueError(
            f"projection keys must exactly match the squad; missing={missing}, unexpected={unexpected}"
        )

    best_starters: tuple[SquadPlayer, ...] | None = None
    best_score = float("-inf")
    for candidate in combinations(squad.players, 11):
        if not is_legal_starting_xi(candidate):
            continue
        score = sum(projection_by_id[player.fpl_id].expected_points for player in candidate)
        # combinations() follows canonical squad_position order. Keeping the
        # first equal-scoring candidate provides a stable, documented tie-break.
        if score > best_score:
            best_score = score
            best_starters = candidate

    if best_starters is None:
        raise ValueError("squad has no legal starting XI")

    starter_ids = {player.fpl_id for player in best_starters}
    bench = tuple(player for player in squad.players if player.fpl_id not in starter_ids)
    bench_goalkeepers = tuple(player for player in bench if player.position == "GK")
    if len(bench_goalkeepers) != 1:
        raise ValueError("recommended bench must contain exactly one goalkeeper")
    outfield_bench = tuple(
        sorted(
            (player for player in bench if player.position != "GK"),
            key=lambda player: (
                -projection_by_id[player.fpl_id].expected_points,
                player.squad_position,
            ),
        )
    )

    captain_order = sorted(
        best_starters,
        key=lambda player: (
            -projection_by_id[player.fpl_id].expected_points,
            player.squad_position,
        ),
    )
    captain, vice_captain = captain_order[:2]
    captain_bonus = projection_by_id[captain.fpl_id].expected_points
    total_xpts = best_score + captain_bonus

    uncertainties = [
        projection_by_id[player.fpl_id].uncertainty for player in best_starters
    ]
    if any(value is None for value in uncertainties):
        combined_uncertainty = None
    else:
        # Independence approximation. The captain contributes twice, so its
        # standard deviation receives a 2x multiplier before variance sums.
        combined_uncertainty = sqrt(
            sum(
                (value * (2.0 if player.fpl_id == captain.fpl_id else 1.0)) ** 2
                for player, value in zip(best_starters, uncertainties, strict=True)
                if value is not None
            )
        )

    flags = sorted(
        {
            flag
            for player in (*best_starters, *outfield_bench, *bench_goalkeepers)
            for flag in projection_by_id[player.fpl_id].data_quality_flags
        }
        | set(squad.constraint_flags)
    )
    return LineupRecommendation(
        starters=tuple(sorted(best_starters, key=lambda player: player.squad_position)),
        bench_goalkeeper=bench_goalkeepers[0],
        outfield_bench_order=outfield_bench,
        captain=captain,
        vice_captain=vice_captain,
        starting_xpts=float(best_score),
        captain_bonus_xpts=float(captain_bonus),
        total_xpts=float(total_xpts),
        uncertainty=combined_uncertainty,
        data_quality_flags=tuple(flags),
    )
