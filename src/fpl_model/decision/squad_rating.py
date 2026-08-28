"""Versioned benchmark-relative squad rating contract.

The rating is deliberately narrower than a catch-all "AI score".  It is an
empirical percentile of optimized-XI-plus-captain xPts against one frozen,
deterministic population of legal squads with the same budget cap.  Release
health, data-quality flags, and projection uncertainty remain separate fields
and never alter that percentile.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, replace
from math import comb, isfinite, prod, sqrt
from random import Random

from fpl_model.decision.lineup import recommend_lineup
from fpl_model.decision.rolling import GameweekProjectionPool
from fpl_model.decision.squad import CHIP_NAMES, POSITION_COUNTS, validate_squad
from fpl_model.decision.transfer import TransferTarget

RATING_SCHEMA_VERSION = "squad_rating_v1"
RATING_FORMULA_VERSION = "optimized_xi_captain_percentile_v1"
BENCHMARK_POLICY_VERSION = "deterministic_rank_weighted_legal_sampler_v1"
DEFAULT_BENCHMARK_POPULATION = 128
DEFAULT_BENCHMARK_MAX_ATTEMPTS = 20_000
DEFAULT_BENCHMARK_SPEND_BAND_TENTHS = 50
MINIMUM_BENCHMARK_POPULATION = 100

_POSITION_ORDER = ("FWD",) * 3 + ("MID",) * 5 + ("DEF",) * 5 + ("GK",) * 2


@dataclass(frozen=True, slots=True)
class SquadBenchmarkRow:
    squad_cost_tenths: int
    gameweek_xpts: tuple[float, ...]
    cumulative_xpts: float
    fpl_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class SquadBenchmark:
    benchmark_id: str
    source_identity: str
    budget_tenths: int
    gameweeks: tuple[int, ...]
    population: tuple[SquadBenchmarkRow, ...]
    eligible_player_count: int
    target_population: int
    max_attempts: int
    spend_band_tenths: int

    @property
    def population_size(self) -> int:
        return len(self.population)

    @property
    def is_eligible(self) -> bool:
        return self.population_size >= MINIMUM_BENCHMARK_POPULATION


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def empirical_percentile(value: float, population: tuple[float, ...]) -> float:
    """Return a deterministic mid-rank empirical percentile in [0, 100]."""

    if not population:
        raise ValueError("percentile population cannot be empty")
    if not isfinite(value) or any(not isfinite(row) for row in population):
        raise ValueError("percentile inputs must be finite")
    less = sum(row < value for row in population)
    equal = sum(row == value for row in population)
    return 100.0 * (less + 0.5 * equal) / len(population)


def _pool_maps(
    pools: tuple[GameweekProjectionPool, ...],
) -> tuple[tuple[dict[int, TransferTarget], ...], tuple[int, ...]]:
    if len(pools) != 3:
        raise ValueError("squad benchmark requires exactly three Gameweek pools")
    gameweeks = tuple(pool.gameweek for pool in pools)
    if gameweeks != tuple(range(gameweeks[0], gameweeks[0] + 3)):
        raise ValueError("squad benchmark Gameweeks must be consecutive")
    maps = tuple({row.player.fpl_id: row for row in pool.players} for pool in pools)
    if any(len(rows) != len(pool.players) for rows, pool in zip(maps, pools, strict=True)):
        raise ValueError("squad benchmark pools contain duplicate player IDs")
    eligible_sets = []
    for rows, pool in zip(maps, pools, strict=True):
        transferable = set(rows) if pool.transferable_fpl_ids is None else set(
            pool.transferable_fpl_ids
        )
        eligible_sets.append(set(rows) & transferable)
    eligible = tuple(sorted(set.intersection(*eligible_sets)))
    for position, required in POSITION_COUNTS.items():
        if sum(maps[0][fpl_id].player.position == position for fpl_id in eligible) < required:
            raise ValueError(f"not enough eligible {position} players for a legal benchmark")
    return maps, eligible


def _rank_by_position(
    maps: tuple[dict[int, TransferTarget], ...], eligible: tuple[int, ...]
) -> tuple[dict[int, int], dict[int, int]]:
    horizon_xpts = {
        fpl_id: sum(rows[fpl_id].projection.expected_points for rows in maps)
        for fpl_id in eligible
    }
    score_rank: dict[int, int] = {}
    value_rank: dict[int, int] = {}
    for position in POSITION_COUNTS:
        ids = [fpl_id for fpl_id in eligible if maps[0][fpl_id].player.position == position]
        by_score = sorted(ids, key=lambda fpl_id: (-horizon_xpts[fpl_id], fpl_id))
        by_value = sorted(
            ids,
            key=lambda fpl_id: (
                -horizon_xpts[fpl_id] / maps[0][fpl_id].player.current_price_tenths,
                -horizon_xpts[fpl_id],
                fpl_id,
            ),
        )
        score_rank.update({fpl_id: index + 1 for index, fpl_id in enumerate(by_score)})
        value_rank.update({fpl_id: index + 1 for index, fpl_id in enumerate(by_value)})
    return score_rank, value_rank


def _sample_squad_ids(
    *,
    attempt: int,
    source_identity: str,
    budget_tenths: int,
    maps: tuple[dict[int, TransferTarget], ...],
    eligible: tuple[int, ...],
    score_rank: dict[int, int],
    value_rank: dict[int, int],
    minimum_remaining_by_slot: tuple[int, ...],
) -> tuple[int, ...] | None:
    by_position = {
        position: tuple(
            fpl_id for fpl_id in eligible if maps[0][fpl_id].player.position == position
        )
        for position in POSITION_COUNTS
    }
    selected: list[int] = []
    selected_set: set[int] = set()
    team_counts: Counter[int] = Counter()
    cost = 0
    lens = attempt % 3
    seed = int.from_bytes(
        hashlib.sha256(
            f"{source_identity}|{budget_tenths}|{attempt}".encode()
        ).digest()[:8],
        "big",
    )
    random = Random(seed)
    for slot, position in enumerate(_POSITION_ORDER):
        viable = []
        for fpl_id in by_position[position]:
            if fpl_id in selected_set:
                continue
            player = maps[0][fpl_id].player
            if team_counts[player.team_id] >= 3:
                continue
            if (
                cost
                + player.current_price_tenths
                + minimum_remaining_by_slot[slot]
                > budget_tenths
            ):
                continue
            rank = (
                score_rank[fpl_id]
                if lens == 0
                else value_rank[fpl_id]
                if lens == 1
                else (score_rank[fpl_id] + value_rank[fpl_id]) / 2
            )
            weight = 1.0 / (4.0 + rank) ** 0.72
            viable.append((fpl_id, weight))
        if not viable:
            return None
        chosen = random.choices(
            [row[0] for row in viable],
            weights=[row[1] for row in viable],
            k=1,
        )[0]
        selected.append(chosen)
        selected_set.add(chosen)
        chosen_player = maps[0][chosen].player
        team_counts[chosen_player.team_id] += 1
        cost += chosen_player.current_price_tenths
    return tuple(sorted(selected))


def _upgrade_sample_to_budget_band(
    fpl_ids: tuple[int, ...],
    *,
    budget_tenths: int,
    spend_band_tenths: int,
    maps: tuple[dict[int, TransferTarget], ...],
    eligible: tuple[int, ...],
    score_rank: dict[int, int],
) -> tuple[int, ...]:
    """Deterministically reinvest spare budget without breaking squad rules."""

    selected = set(fpl_ids)
    cost = sum(maps[0][fpl_id].player.current_price_tenths for fpl_id in selected)
    while cost < budget_tenths - spend_band_tenths:
        team_counts = Counter(maps[0][fpl_id].player.team_id for fpl_id in selected)
        swaps: list[tuple[int, int, int, int]] = []
        for outgoing_id in sorted(selected):
            outgoing = maps[0][outgoing_id].player
            for incoming_id in eligible:
                if incoming_id in selected:
                    continue
                incoming = maps[0][incoming_id].player
                if incoming.position != outgoing.position:
                    continue
                increase = incoming.current_price_tenths - outgoing.current_price_tenths
                if increase <= 0 or cost + increase > budget_tenths:
                    continue
                incoming_team_count = team_counts[incoming.team_id]
                if incoming.team_id != outgoing.team_id and incoming_team_count >= 3:
                    continue
                # Avoid buying a dramatically weaker player merely to burn
                # budget; the sampler can still be diverse within a broad
                # 25-rank position window.
                if score_rank[incoming_id] > score_rank[outgoing_id] + 25:
                    continue
                swaps.append((-increase, score_rank[incoming_id], outgoing_id, incoming_id))
        if not swaps:
            break
        _, _, outgoing_id, incoming_id = min(swaps)
        outgoing_price = maps[0][outgoing_id].player.current_price_tenths
        incoming_price = maps[0][incoming_id].player.current_price_tenths
        selected.remove(outgoing_id)
        selected.add(incoming_id)
        cost += incoming_price - outgoing_price
    return tuple(sorted(selected))


def _validated_benchmark_squad(
    fpl_ids: tuple[int, ...],
    *,
    maps: tuple[dict[int, TransferTarget], ...],
    budget_tenths: int,
):
    by_position = {
        position: sorted(
            (maps[0][fpl_id].player for fpl_id in fpl_ids if maps[0][fpl_id].player.position == position),
            key=lambda player: player.fpl_id,
        )
        for position in POSITION_COUNTS
    }
    starters = (
        by_position["GK"][:1]
        + by_position["DEF"][:3]
        + by_position["MID"][:4]
        + by_position["FWD"][:3]
    )
    starter_ids = {player.fpl_id for player in starters}
    bench = [
        maps[0][fpl_id].player for fpl_id in fpl_ids if fpl_id not in starter_ids
    ]
    ordered = (*starters, *bench)
    players = tuple(
        replace(
            player,
            squad_position=index,
            is_captain=index == 1,
            is_vice_captain=index == 2,
        )
        for index, player in enumerate(ordered, start=1)
    )
    cost = sum(player.current_price_tenths for player in players)
    return validate_squad(
        players,
        bank_tenths=budget_tenths - cost,
        free_transfers=None,
        unlimited_transfers=True,
        chip_period=1,
        chip_states=dict.fromkeys(CHIP_NAMES, "available"),
    )


def build_squad_benchmark(
    pools: tuple[GameweekProjectionPool, ...],
    *,
    source_identity: str,
    budget_tenths: int,
    target_population: int = DEFAULT_BENCHMARK_POPULATION,
    max_attempts: int = DEFAULT_BENCHMARK_MAX_ATTEMPTS,
    spend_band_tenths: int = DEFAULT_BENCHMARK_SPEND_BAND_TENTHS,
) -> SquadBenchmark:
    """Build a fast, frozen legal-squad population for an exact budget cap."""

    if not source_identity.strip():
        raise ValueError("source_identity cannot be blank")
    if budget_tenths <= 0 or target_population <= 0 or max_attempts <= 0:
        raise ValueError("benchmark budget, population, and attempts must be positive")
    if spend_band_tenths < 0:
        raise ValueError("benchmark spend band cannot be negative")
    maps, eligible = _pool_maps(pools)
    score_rank, value_rank = _rank_by_position(maps, eligible)
    prices_by_position = {
        position: sorted(
            maps[0][fpl_id].player.current_price_tenths
            for fpl_id in eligible
            if maps[0][fpl_id].player.position == position
        )
        for position in POSITION_COUNTS
    }
    minimum_remaining_by_slot = tuple(
        sum(
            sum(prices_by_position[position][:count])
            for position, count in Counter(_POSITION_ORDER[slot + 1 :]).items()
        )
        for slot in range(len(_POSITION_ORDER))
    )
    position_eligible_counts = Counter(
        maps[0][fpl_id].player.position for fpl_id in eligible
    )
    combinatorial_upper_bound = prod(
        comb(position_eligible_counts[position], required)
        for position, required in POSITION_COUNTS.items()
    )
    retained_ids: list[tuple[int, ...]] = []
    if combinatorial_upper_bound >= MINIMUM_BENCHMARK_POPULATION:
        sampled: dict[tuple[int, ...], int] = {}
        last_new_attempt = 0
        for attempt in range(max_attempts):
            fpl_ids = _sample_squad_ids(
                attempt=attempt,
                source_identity=source_identity,
                budget_tenths=budget_tenths,
                maps=maps,
                eligible=eligible,
                score_rank=score_rank,
                value_rank=value_rank,
                minimum_remaining_by_slot=minimum_remaining_by_slot,
            )
            if fpl_ids is None or fpl_ids in sampled:
                if attempt - last_new_attempt >= max(200, target_population):
                    break
                continue
            fpl_ids = _upgrade_sample_to_budget_band(
                fpl_ids,
                budget_tenths=budget_tenths,
                spend_band_tenths=spend_band_tenths,
                maps=maps,
                eligible=eligible,
                score_rank=score_rank,
            )
            if fpl_ids in sampled:
                if attempt - last_new_attempt >= max(200, target_population):
                    break
                continue
            sampled[fpl_ids] = sum(
                maps[0][fpl_id].player.current_price_tenths for fpl_id in fpl_ids
            )
            last_new_attempt = attempt
            if len(sampled) >= target_population * 2:
                break
        if not sampled:
            raise ValueError("deterministic sampler found no legal same-budget squads")
        effective_ceiling = min(budget_tenths, max(sampled.values()))
        spend_floor = effective_ceiling - spend_band_tenths
        preferred_ids = [
            fpl_ids for fpl_ids, cost in sampled.items() if cost >= spend_floor
        ]
        preferred_set = set(preferred_ids)
        backfill_ids = [
            fpl_ids
            for fpl_ids, _ in sorted(sampled.items(), key=lambda row: (-row[1], row[0]))
            if fpl_ids not in preferred_set
        ]
        retained_ids = (preferred_ids + backfill_ids)[:target_population]
    population = tuple(
        SquadBenchmarkRow(
            squad_cost_tenths=sum(
                maps[0][fpl_id].player.current_price_tenths for fpl_id in fpl_ids
            ),
            gameweek_xpts=gameweek_xpts,
            cumulative_xpts=sum(gameweek_xpts),
            fpl_ids=fpl_ids,
        )
        for fpl_ids in retained_ids
        for squad in (_validated_benchmark_squad(
            fpl_ids, maps=maps, budget_tenths=budget_tenths
        ),)
        for gameweek_xpts in (
            tuple(
                recommend_lineup(
                    squad,
                    tuple(maps[index][fpl_id].projection for fpl_id in fpl_ids),
                ).total_xpts
                for index in range(3)
            ),
        )
    )
    gameweeks = tuple(pool.gameweek for pool in pools)
    identity_inputs = {
        "schema_version": RATING_SCHEMA_VERSION,
        "formula_version": RATING_FORMULA_VERSION,
        "population_policy_version": BENCHMARK_POLICY_VERSION,
        "source_identity": source_identity,
        "budget_tenths": budget_tenths,
        "gameweeks": gameweeks,
        "target_population": target_population,
        "max_attempts": max_attempts,
        "spend_band_tenths": spend_band_tenths,
        "eligible_player_ids": eligible,
        "combinatorial_upper_bound": combinatorial_upper_bound,
        "population": [
            {
                "cost": row.squad_cost_tenths,
                "fpl_ids": row.fpl_ids,
                "gameweek_xpts": row.gameweek_xpts,
                "cumulative_xpts": row.cumulative_xpts,
            }
            for row in population
        ],
    }
    return SquadBenchmark(
        benchmark_id=f"squad_benchmark_{_canonical_digest(identity_inputs)[:16]}",
        source_identity=source_identity,
        budget_tenths=budget_tenths,
        gameweeks=gameweeks,
        population=population,
        eligible_player_count=len(eligible),
        target_population=target_population,
        max_attempts=max_attempts,
        spend_band_tenths=spend_band_tenths,
    )


def rate_squad(
    benchmark: SquadBenchmark,
    *,
    raw_gameweek_xpts: tuple[float, ...],
    gameweek_uncertainty: tuple[float | None, ...],
    quality_flags: tuple[str, ...],
    squad_rule_flags: tuple[str, ...],
    release_health: str,
    reviewed_scenario: bool,
) -> dict[str, object]:
    """Return the auditable rating payload without blending health dimensions."""

    if len(raw_gameweek_xpts) != len(benchmark.gameweeks):
        raise ValueError("raw Gameweek xPts must match the benchmark horizon")
    if len(gameweek_uncertainty) != len(benchmark.gameweeks):
        raise ValueError("Gameweek uncertainty must match the benchmark horizon")
    cumulative_xpts = sum(raw_gameweek_xpts)
    base = {
        "schema_version": RATING_SCHEMA_VERSION,
        "display_label": "Model Score" if release_health == "production" else "Model Preview",
        "formula_version": RATING_FORMULA_VERSION,
        "benchmark": {
            "benchmark_id": benchmark.benchmark_id,
            "population_policy_version": BENCHMARK_POLICY_VERSION,
            "source_identity": benchmark.source_identity,
            "budget_tenths": benchmark.budget_tenths,
            "population_size": benchmark.population_size,
            "minimum_population": MINIMUM_BENCHMARK_POPULATION,
            "eligible_player_count": benchmark.eligible_player_count,
            "target_population": benchmark.target_population,
            "max_attempts": benchmark.max_attempts,
            "spend_band_tenths": benchmark.spend_band_tenths,
        },
        "input": {
            "gameweeks": list(benchmark.gameweeks),
            "raw_gameweek_xpts": list(raw_gameweek_xpts),
            "raw_cumulative_xpts": cumulative_xpts,
            "reviewed_scenario": reviewed_scenario,
        },
        "release_gate": {
            "health": release_health,
            "production_approved": release_health == "production",
        },
        "data_confidence": {
            "state": "review" if quality_flags else "clean",
            "quality_flags": list(quality_flags),
        },
        "projection_uncertainty": {
            "gameweek": [
                {"gameweek": gameweek, "uncertainty": uncertainty}
                for gameweek, uncertainty in zip(
                    benchmark.gameweeks, gameweek_uncertainty, strict=True
                )
            ],
            "cumulative_rss": (
                None
                if any(value is None for value in gameweek_uncertainty)
                else sqrt(sum(value**2 for value in gameweek_uncertainty if value is not None))
            ),
        },
        "squad_rule_health": {
            "state": "pass" if not squad_rule_flags else "review",
            "flags": list(squad_rule_flags),
        },
    }
    if not benchmark.is_eligible:
        return {
            **base,
            "available": False,
            "model_strength": None,
            "explanation": (
                "Rating withheld: the frozen legal-squad benchmark is smaller than the "
                f"minimum {MINIMUM_BENCHMARK_POPULATION}-squad population. Raw xPts remain "
                "available."
            ),
        }

    per_gameweek = [
        {
            "gameweek": gameweek,
            "raw_xpts": value,
            "percentile": empirical_percentile(
                value,
                tuple(row.gameweek_xpts[index] for row in benchmark.population),
            ),
        }
        for index, (gameweek, value) in enumerate(
            zip(benchmark.gameweeks, raw_gameweek_xpts, strict=True)
        )
    ]
    overall_percentile = empirical_percentile(
        cumulative_xpts,
        tuple(row.cumulative_xpts for row in benchmark.population),
    )
    return {
        **base,
        "available": True,
        "model_strength": {
            "per_gameweek": per_gameweek,
            "overall_3gw": {
                "raw_cumulative_xpts": cumulative_xpts,
                "percentile": overall_percentile,
            },
        },
        "explanation": (
            "Percentile versus a deterministic population of legal squads built with the "
            "same current-price budget cap. Each Gameweek uses optimized XI plus captain; "
            "the 3GW percentile is calculated from cumulative raw xPts, never from an "
            "average of rounded Gameweek ratings. Confidence, uncertainty, squad rules, "
            "and release approval are reported separately."
        ),
    }
