from __future__ import annotations

import pytest

from fpl_model.decision.squad_rating import (
    MATERIALIZED_BENCHMARK_SCHEMA_VERSION,
    MINIMUM_BENCHMARK_POPULATION,
    SquadBenchmark,
    SquadBenchmarkRow,
    benchmark_from_materialized_artifact,
    build_materialized_benchmark_artifact,
    build_squad_benchmark,
    empirical_percentile,
    rate_squad,
)
from tests.test_initial_squad import _pools


def _benchmark() -> SquadBenchmark:
    rows = tuple(
        SquadBenchmarkRow(
            squad_cost_tenths=1_000,
            gameweek_xpts=(40.0 + index, 70.0 - index / 2, 45.0 + index / 4),
            cumulative_xpts=155.0 + index * 0.75,
        )
        for index in range(MINIMUM_BENCHMARK_POPULATION)
    )
    return SquadBenchmark(
        benchmark_id="squad_benchmark_test",
        source_identity="web_release_test",
        budget_tenths=1_000,
        gameweeks=(2, 3, 4),
        population=rows,
        eligible_player_count=600,
        target_population=MINIMUM_BENCHMARK_POPULATION,
        max_attempts=1_000,
        spend_band_tenths=50,
    )


def test_empirical_percentile_is_monotonic_and_uses_midrank_for_ties():
    population = (1.0, 2.0, 2.0, 4.0)

    assert empirical_percentile(0.0, population) == 0.0
    assert empirical_percentile(2.0, population) == 50.0
    assert empirical_percentile(5.0, population) == 100.0
    assert empirical_percentile(2.1, population) > empirical_percentile(2.0, population)


def test_benchmark_is_reproducible_and_every_squad_uses_the_same_budget_cap():
    first = build_squad_benchmark(
        _pools(),
        source_identity="web_release_test",
        budget_tenths=1_000,
        target_population=20,
        max_attempts=500,
    )
    second = build_squad_benchmark(
        _pools(),
        source_identity="web_release_test",
        budget_tenths=1_000,
        target_population=20,
        max_attempts=500,
    )

    assert first == second
    assert first.benchmark_id.startswith("squad_benchmark_")

    assert all(row.squad_cost_tenths <= first.budget_tenths for row in first.population)


def test_overall_rating_uses_cumulative_population_not_mean_of_gw_percentiles():
    result = rate_squad(
        _benchmark(),
        raw_gameweek_xpts=(70.0, 42.0, 52.0),
        gameweek_uncertainty=(2.0, 3.0, 4.0),
        quality_flags=("ROLE_STATE_REVIEW",),
        squad_rule_flags=(),
        release_health="shadow",
        reviewed_scenario=False,
    )

    assert result["available"] is True
    strength = result["model_strength"]
    per_gameweek = strength["per_gameweek"]
    overall = strength["overall_3gw"]
    arithmetic_mean = sum(row["percentile"] for row in per_gameweek) / 3
    assert overall["raw_cumulative_xpts"] == 164.0
    assert overall["percentile"] != pytest.approx(arithmetic_mean)
    assert result["display_label"] == "Model Preview"
    assert result["data_confidence"]["state"] == "review"
    assert result["projection_uncertainty"]["cumulative_rss"] == pytest.approx(29**0.5)
    assert result["squad_rule_health"]["state"] == "pass"
    assert result["release_gate"]["production_approved"] is False


def test_small_population_withholds_percentile_but_keeps_raw_xpts():
    benchmark = _benchmark()
    benchmark = SquadBenchmark(
        benchmark_id=benchmark.benchmark_id,
        source_identity=benchmark.source_identity,
        budget_tenths=benchmark.budget_tenths,
        gameweeks=benchmark.gameweeks,
        population=benchmark.population[: MINIMUM_BENCHMARK_POPULATION - 1],
        eligible_player_count=benchmark.eligible_player_count,
        target_population=benchmark.target_population,
        max_attempts=benchmark.max_attempts,
        spend_band_tenths=benchmark.spend_band_tenths,
    )

    result = rate_squad(
        benchmark,
        raw_gameweek_xpts=(50.0, 51.0, 52.0),
        gameweek_uncertainty=(None, None, None),
        quality_flags=(),
        squad_rule_flags=(),
        release_health="production",
        reviewed_scenario=True,
    )

    assert result["available"] is False
    assert result["model_strength"] is None
    assert result["input"]["raw_cumulative_xpts"] == 153.0
    assert result["display_label"] == "Model Score"
    assert "withheld" in result["explanation"].lower()


def test_materialized_master_selects_a_reproducible_exact_budget_population():
    population = [
        {
            "fpl_ids": list(range(index * 20 + 1, index * 20 + 16)),
            "squad_cost_tenths": 900 + index % 101,
            "gameweek_xpts": [40.0 + index / 10, 41.0 + index / 10, 42.0 + index / 10],
            "cumulative_xpts": 123.0 + index * 0.3,
        }
        for index in range(300)
    ]
    artifact = {
        "schema_version": MATERIALIZED_BENCHMARK_SCHEMA_VERSION,
        "formula_version": "optimized_xi_captain_percentile_v1",
        "population_policy_version": "deterministic_rank_weighted_legal_sampler_v1",
        "artifact_id": "squad_benchmark_master_test",
        "status": "ready",
        "source_identity": "rating_source_test",
        "gameweeks": [2, 3, 4],
        "population": population,
        "target_population_per_anchor": 128,
        "max_attempts_per_anchor": 20_000,
        "spend_band_tenths": 50,
        "eligible_player_count": 600,
    }

    first = benchmark_from_materialized_artifact(artifact, budget_tenths=980)
    second = benchmark_from_materialized_artifact(artifact, budget_tenths=980)

    assert first == second
    assert first.population_size == 128
    assert first.materialization_mode == "release_artifact"
    assert all(row.squad_cost_tenths <= 980 for row in first.population)
    assert first.benchmark_id.startswith("squad_benchmark_")

    stale = dict(artifact)
    stale["formula_version"] = "obsolete_formula"
    with pytest.raises(ValueError, match="formula version is incompatible"):
        benchmark_from_materialized_artifact(stale, budget_tenths=980)


# --- Sprint 7 sign-off: monotonicity, rerun stability, and input sensitivity ---
#
# The existing tests above prove the percentile formula itself and reproducibility
# of one `build_squad_benchmark`/`benchmark_from_materialized_artifact` call. These
# close the remaining Sprint 7 bullet: a rating must never reward a strictly worse
# squad, must be bit-for-bit stable when the exact same release is rated again, and
# must move in the correct direction (never invert) as captaincy, bench structure,
# injuries, or fixtures change the raw xPts feeding it.


def test_rating_is_monotonic_in_raw_cumulative_xpts_on_a_fixed_benchmark():
    """A strictly higher cumulative xPts must never score a strictly lower percentile.

    This is the general property the tie-handling test above only spot-checks at
    one pair of values: sweep a wide range of cumulative totals, including values
    that collide with population rows, and assert percentile never decreases as
    the input strictly increases.
    """

    benchmark = _benchmark()
    totals = [round(140.0 + step * 0.37, 2) for step in range(120)]

    percentiles = [
        rate_squad(
            benchmark,
            raw_gameweek_xpts=(total / 3, total / 3, total / 3),
            gameweek_uncertainty=(None, None, None),
            quality_flags=(),
            squad_rule_flags=(),
            release_health="shadow",
            reviewed_scenario=False,
        )["model_strength"]["overall_3gw"]["percentile"]
        for total in totals
    ]

    assert all(
        later >= earlier for earlier, later in zip(percentiles, percentiles[1:], strict=False)
    )
    # And the sweep must actually discriminate somewhere, not just be flat/withheld.
    assert percentiles[0] < percentiles[-1]


def test_rating_is_stable_across_reruns_of_the_same_frozen_release():
    """Rating the same release twice, from scratch, must reproduce identical output.

    Exercises the real end-to-end path a release refresh and a web request each
    take -- materialize once, then select/rate independently -- rather than only
    the narrower `build_squad_benchmark`-vs-itself check above.
    """

    artifact = build_materialized_benchmark_artifact(
        _pools(),
        source_identity="rating_stability_test",
        budget_anchors=(1_000,),
        target_population_per_anchor=100,
        max_attempts_per_anchor=2_000,
    )
    assert artifact["status"] == "ready"

    def _rate() -> dict[str, object]:
        benchmark = benchmark_from_materialized_artifact(artifact, budget_tenths=1_000)
        return rate_squad(
            benchmark,
            raw_gameweek_xpts=(45.0, 46.0, 47.0),
            gameweek_uncertainty=(1.5, 1.5, 1.5),
            quality_flags=(),
            squad_rule_flags=(),
            release_health="production",
            reviewed_scenario=False,
        )

    first = _rate()
    second = _rate()

    assert first == second
    assert first["benchmark"]["benchmark_id"] == second["benchmark"]["benchmark_id"]


@pytest.mark.parametrize(
    "delta,expected",
    [
        (6.0, "not_worse"),  # a captaincy upgrade or bench-structure improvement
        (-6.0, "not_better"),  # an injury or a fixture turning unfavourable
    ],
)
def test_rating_moves_in_the_correct_direction_when_raw_xpts_are_perturbed(delta, expected):
    """Sensitivity to captaincy/bench/injury/fixture changes must never invert.

    A caller who swaps the captain armband, promotes a bench player into the XI,
    or revises a projection down after an injury/unfavourable fixture change only
    ever changes the raw Gameweek xPts fed into `rate_squad` -- the rating itself
    has no separate knobs for those events. So the contract to prove here is: the
    percentile moves in the same direction as the raw input, never the opposite.
    """

    benchmark = _benchmark()
    baseline_total = 164.0

    def _percentile(total: float) -> float:
        result = rate_squad(
            benchmark,
            raw_gameweek_xpts=(total / 3, total / 3, total / 3),
            gameweek_uncertainty=(None, None, None),
            quality_flags=(),
            squad_rule_flags=(),
            release_health="production",
            reviewed_scenario=True,
        )
        return result["model_strength"]["overall_3gw"]["percentile"]

    before = _percentile(baseline_total)
    after = _percentile(baseline_total + delta)

    if expected == "not_worse":
        assert after >= before
    else:
        assert after <= before


def test_provisional_to_final_health_transition_never_changes_the_percentile():
    """Release health (research/shadow/production) must stay orthogonal to the rating.

    Sprint 7 requires checking drift as a release moves from provisional to final;
    the explicit design contract (see the module docstring) is that release health
    and data-quality flags never blend into the percentile itself -- only the
    `display_label` and `release_gate.production_approved` fields change. Prove
    that invariant directly so a future change cannot silently couple them.
    """

    benchmark = _benchmark()
    raw = (55.0, 56.0, 57.0)

    results = {
        health: rate_squad(
            benchmark,
            raw_gameweek_xpts=raw,
            gameweek_uncertainty=(None, None, None),
            quality_flags=(),
            squad_rule_flags=(),
            release_health=health,
            reviewed_scenario=False,
        )
        for health in ("research", "shadow", "production")
    }

    percentiles = {
        health: result["model_strength"]["overall_3gw"]["percentile"]
        for health, result in results.items()
    }
    assert len(set(percentiles.values())) == 1

    assert results["production"]["display_label"] == "Model Score"
    assert results["research"]["display_label"] == "Model Preview"
    assert results["shadow"]["display_label"] == "Model Preview"
    assert results["production"]["release_gate"]["production_approved"] is True
    assert results["research"]["release_gate"]["production_approved"] is False
