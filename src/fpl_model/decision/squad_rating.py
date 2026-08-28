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
MATERIALIZED_BENCHMARK_SCHEMA_VERSION = "squad_benchmark_master_v1"
DEFAULT_MATERIALIZED_BUDGET_ANCHORS = (900, 950, 1_000, 1_050, 1_100, 1_150)

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
    materialization_mode: str = "runtime_cache"

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


def build_materialized_benchmark_artifact(
    pools: tuple[GameweekProjectionPool, ...],
    *,
    source_identity: str,
    budget_anchors: tuple[int, ...] = DEFAULT_MATERIALIZED_BUDGET_ANCHORS,
    target_population_per_anchor: int = DEFAULT_BENCHMARK_POPULATION,
    max_attempts_per_anchor: int = DEFAULT_BENCHMARK_MAX_ATTEMPTS,
    spend_band_tenths: int = DEFAULT_BENCHMARK_SPEND_BAND_TENTHS,
) -> dict[str, object]:
    """Materialize reusable scored squads during release refresh, not web requests."""

    if not budget_anchors or tuple(sorted(set(budget_anchors))) != budget_anchors:
        raise ValueError("materialized benchmark budget anchors must be unique and ascending")
    rows_by_squad: dict[tuple[int, ...], SquadBenchmarkRow] = {}
    anchor_reports: list[dict[str, object]] = []
    problems: list[str] = []
    eligible_player_count = 0
    gameweeks = tuple(pool.gameweek for pool in pools)
    for budget_tenths in budget_anchors:
        try:
            benchmark = build_squad_benchmark(
                pools,
                source_identity=source_identity,
                budget_tenths=budget_tenths,
                target_population=target_population_per_anchor,
                max_attempts=max_attempts_per_anchor,
                spend_band_tenths=spend_band_tenths,
            )
        except ValueError as error:
            anchor_reports.append(
                {
                    "budget_tenths": budget_tenths,
                    "population_size": 0,
                    "status": "unavailable",
                    "reason": str(error),
                }
            )
            problems.append(f"budget {budget_tenths}: {error}")
            continue
        eligible_player_count = max(eligible_player_count, benchmark.eligible_player_count)
        for row in benchmark.population:
            rows_by_squad[row.fpl_ids] = row
        anchor_status = "ready" if benchmark.is_eligible else "unavailable"
        anchor_reports.append(
            {
                "budget_tenths": budget_tenths,
                "population_size": benchmark.population_size,
                "status": anchor_status,
            }
        )
        if anchor_status != "ready":
            problems.append(
                f"budget {budget_tenths}: population {benchmark.population_size} is below "
                f"minimum {MINIMUM_BENCHMARK_POPULATION}"
            )

    population = [
        {
            "fpl_ids": list(row.fpl_ids),
            "squad_cost_tenths": row.squad_cost_tenths,
            "gameweek_xpts": list(row.gameweek_xpts),
            "cumulative_xpts": row.cumulative_xpts,
        }
        for _, row in sorted(rows_by_squad.items())
    ]
    ready_anchors = sum(row["status"] == "ready" for row in anchor_reports)
    unsigned = {
        "schema_version": MATERIALIZED_BENCHMARK_SCHEMA_VERSION,
        "formula_version": RATING_FORMULA_VERSION,
        "population_policy_version": BENCHMARK_POLICY_VERSION,
        "source_identity": source_identity,
        "status": "ready" if ready_anchors == len(budget_anchors) else "unavailable",
        "gameweeks": list(gameweeks),
        "budget_anchors_tenths": list(budget_anchors),
        "target_population_per_anchor": target_population_per_anchor,
        "minimum_runtime_population": MINIMUM_BENCHMARK_POPULATION,
        "max_attempts_per_anchor": max_attempts_per_anchor,
        "spend_band_tenths": spend_band_tenths,
        "eligible_player_count": eligible_player_count,
        "anchor_reports": anchor_reports,
        "population": population,
        "problems": problems,
    }
    return {
        **unsigned,
        "artifact_id": f"squad_benchmark_master_{_canonical_digest(unsigned)[:16]}",
    }


def benchmark_from_materialized_artifact(
    artifact: dict[str, object], *, budget_tenths: int
) -> SquadBenchmark:
    """Select one exact-budget benchmark from a frozen master population."""

    if artifact.get("schema_version") != MATERIALIZED_BENCHMARK_SCHEMA_VERSION:
        raise ValueError("unsupported materialized squad benchmark schema")
    if artifact.get("formula_version") != RATING_FORMULA_VERSION:
        raise ValueError("materialized squad benchmark formula version is incompatible")
    if artifact.get("population_policy_version") != BENCHMARK_POLICY_VERSION:
        raise ValueError("materialized squad benchmark population policy is incompatible")
    if artifact.get("status") != "ready":
        raise ValueError("materialized squad benchmark is not ready")
    if budget_tenths <= 0:
        raise ValueError("benchmark budget must be positive")
    raw_population = artifact.get("population")
    if not isinstance(raw_population, list):
        raise ValueError("materialized squad benchmark population is missing")
    rows: list[SquadBenchmarkRow] = []
    for raw in raw_population:
        if not isinstance(raw, dict):
            raise ValueError("materialized squad benchmark row must be an object")
        cost = int(raw["squad_cost_tenths"])
        if cost > budget_tenths:
            continue
        gameweek_xpts = tuple(float(value) for value in raw["gameweek_xpts"])
        fpl_ids = tuple(int(value) for value in raw["fpl_ids"])
        rows.append(
            SquadBenchmarkRow(
                squad_cost_tenths=cost,
                gameweek_xpts=gameweek_xpts,
                cumulative_xpts=float(raw["cumulative_xpts"]),
                fpl_ids=fpl_ids,
            )
        )
    spend_band_tenths = int(artifact["spend_band_tenths"])
    target_population = int(artifact["target_population_per_anchor"])
    rows.sort(
        key=lambda row: (
            row.squad_cost_tenths < budget_tenths - spend_band_tenths,
            -row.squad_cost_tenths,
            row.fpl_ids,
        )
    )
    population = tuple(rows[:target_population])
    source_identity = str(artifact["source_identity"])
    gameweeks = tuple(int(value) for value in artifact["gameweeks"])
    identity_inputs = {
        "schema_version": RATING_SCHEMA_VERSION,
        "formula_version": RATING_FORMULA_VERSION,
        "population_policy_version": BENCHMARK_POLICY_VERSION,
        "master_artifact_id": artifact.get("artifact_id"),
        "source_identity": source_identity,
        "budget_tenths": budget_tenths,
        "population": [
            {
                "fpl_ids": row.fpl_ids,
                "cost": row.squad_cost_tenths,
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
        eligible_player_count=int(artifact["eligible_player_count"]),
        target_population=target_population,
        max_attempts=int(artifact["max_attempts_per_anchor"]),
        spend_band_tenths=spend_band_tenths,
        materialization_mode="release_artifact",
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
            "materialization_mode": benchmark.materialization_mode,
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
