"""Causal walk-forward execution of appearance calibration policies chosen from 2025-26 diagnostics.

``scripts/diagnose_appearance_segments.py`` found the appearance model's
average overprediction bias reliably concentrated in the highest
start_probability/expected_minutes bands and reliably larger in the xPts high
band -- but that diagnostic never recomputed predicted_xpts under any
calibration, so it cannot say whether actually applying one would help. This
script does: it materializes three policies from ONE shared walk-forward pass
(``validation.appearance_policy_backtest.materialize_appearance_policy_backtest``)
and scores them head-to-head, out of sample, against ``actual_points``:

- ``raw``: the existing, committed model -- unmodified, the control. Its own
  aggregate metrics are checked against ``--reference``
  (defaulting to the committed ``docs/research/walk_forward_benchwarmers_2025_26.json``)
  before anything else runs; a ROW-LEVEL parity check against the canonical
  materializer's own output (``raw_row_level_parity``) additionally confirms
  EVERY field of every observation and gap, plus every count, agrees exactly
  -- the aggregate self-check alone cannot rule out two different row-level
  results sharing the same aggregate mean/sum-of-squares.
- ``global``: the causal walk-forward start_probability calibration fit
  applied to every row.
- ``high_end_shrinkage``: that same fit applied only to rows whose RAW
  start_probability is >= 0.8 (the fixed band edge
  ``validation.appearance_segments`` already uses).

IMPORTANT -- what "causal" does and does NOT mean here: every individual
fit and threshold application is genuinely chronological (fit ONLY from
strictly-prior, deadline-safe gameweeks; the 0.8 threshold applied
identically at every step, never adjusted mid-run). That is walk-forward
execution, and it is causal in the narrow sense that no future gameweek's
outcome ever enters an earlier fit. It does NOT mean the segment
diagnostic's findings "cannot leak" into this evaluation in the broader
sense: the DECISION to test exactly these three policies -- and especially
the decision to test high-end-only shrinkage as a candidate at all -- was
made after inspecting ``diagnose_appearance_segments.py``'s own 2025-26
results, then evaluated on that SAME 2025-26 season again. The literal 0.8
boundary is a predefined, reused segment-band edge, not a value fit from
this run's own data; but the choice to turn that observed segment finding
into a candidate policy was still informed by this season, making this an
EXPLORATORY, same-season policy comparison, not an independent
confirmatory one. This script's own bootstrap CIs quantify sampling
uncertainty in the paired MAE/RMSE comparison ONLY -- they do NOT account
for this policy/model-selection uncertainty (the fact that "high-end
shrinkage" was one candidate among many that could have been proposed, its
being tested at all is itself conditioned on this season's diagnostic).
Any verdict below (e.g. "global is better-supported") should be read as
better-supported WITHIN this exploratory 2025-26 comparison -- an
independent season, or a prospectively frozen 2026-27 evaluation specified
BEFORE that season's results are known, is required before adopting any
policy in production.

expected_minutes OLS calibration is out of scope -- it has no causal path to
predicted_xpts under the current formula (see
``validation.appearance_policy_backtest.EXPECTED_MINUTES_OUT_OF_SCOPE_NOTE``).
Every tested policy is NOT a pure start_probability substitution: each
applies an OLS transform to start_probability and then proportionally
rescales the dependent substitute/60-minute probabilities (and, for internal
consistency, expected_minutes) -- see
``validation.appearance_policy_backtest.rescale_appearance_projection``. The
performance results below evaluate that complete reconstruction rule.

Paired, gameweek-cluster bootstrap contrasts (reusing
``validation.paired_uncertainty.build_paired_rows``/``cluster_bootstrap`` --
the SAME primitive the model-vs-naive comparator already uses, since all
three policies score identical candidate rows by construction) answer:
global vs raw, high_end_shrinkage vs raw, and high_end_shrinkage vs global,
for both MAE and RMSE improvement.

Read-only measurement: no calibration is applied to any production
projection, and adopting a policy in production remains a separate, explicit
decision this script does not make -- and, per the exploratory-comparison
caveat above, should not be made on this run's evidence alone.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import pandas as pd

from fpl_model.ingest.player_history import import_player_fixture_history
from fpl_model.ingest.vaastav import VaastavClient
from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.appearance_policy_backtest import (
    DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS,
    EXPECTED_MINUTES_OUT_OF_SCOPE_NOTE,
    HIGH_END_START_PROBABILITY_THRESHOLD,
    POLICIES,
    PolicyName,
    materialize_appearance_policy_backtest,
    verify_raw_row_level_parity,
)
from fpl_model.validation.backtest import score_predictions
from fpl_model.validation.backtest_self_check import load_reference, verify_self_check
from fpl_model.validation.benchwarmers_backtest import (
    materialize_benchwarmers_walk_forward_backtest,
)
from fpl_model.validation.paired_uncertainty import (
    DEFAULT_RESAMPLES,
    DEFAULT_SEED,
    build_paired_rows,
    cluster_bootstrap,
)

POLICY_COMPARISONS: tuple[tuple[PolicyName, PolicyName], ...] = (
    ("global", "raw"),
    ("high_end_shrinkage", "raw"),
    ("high_end_shrinkage", "global"),
)

LIMITATIONS = [
    "expected_minutes OLS calibration is out of scope: it has no causal path to predicted_xpts "
    "under the current formula (only start_probability and its dependent fields reach any "
    "scoring component) -- see expected_minutes_out_of_scope_note. expected_minutes IS still "
    "recomputed (not passed through unchanged) for internal projection consistency, but that "
    "recomputation has no independently fitted parameters and cannot move any score either.",
    "Every tested policy is NOT a pure start_probability substitution: each applies an OLS "
    "transform to start_probability and proportionally rescales the dependent substitute/"
    "60-minute probabilities (and expected_minutes) -- the performance results evaluate that "
    "complete reconstruction rule, not calibrated start_probability in isolation. See "
    "validation.appearance_policy_backtest.rescale_appearance_projection.",
    "This is CAUSAL WALK-FORWARD EXECUTION of an EXPLORATORY, SAME-SEASON policy "
    "specification, not an independent confirmatory backtest: the high_end_shrinkage "
    "threshold (0.8) and the start_probability OLS calibration fit at every gameweek are "
    "fixed constants / fit ONLY from strictly-prior, deadline-safe gameweeks -- so no future "
    "gameweek's OUTCOME ever enters an earlier fit -- but the DECISION to test high-end-only "
    "shrinkage as a candidate policy at all was made after inspecting "
    "diagnose_appearance_segments.py's own 2025-26 findings, then evaluated on that SAME "
    "2025-26 season again. This script's bootstrap CIs quantify sampling uncertainty in the "
    "paired MAE/RMSE comparison ONLY -- they do NOT account for this policy/model-selection "
    "uncertainty. Any verdict is better-supported WITHIN this exploratory 2025-26 comparison, "
    "not independently confirmed; an independent season or a prospectively frozen 2026-27 "
    "evaluation (policy specified before that season's results are known) is required before "
    "production adoption.",
    "raw's own aggregate metrics are checked against --reference, AND its individual rows are "
    "checked against the canonical materializer's own output field-by-field "
    "(raw_row_level_parity) -- the aggregate check alone cannot rule out two different "
    "row-level results sharing the same aggregate mean/sum-of-squares. Both checks run before "
    "any policy comparison is trusted; a mismatch in either means this script's duplicated "
    "walk-forward loop has diverged from the committed materializer, and nothing is written.",
    "Paired bootstrap contrasts compare MAE/RMSE improvement (naive here meaning the OTHER "
    "policy in the pair, not the unrelated matched-naive comparator elsewhere in this "
    "codebase) using the gameweek-cluster scheme validation.paired_uncertainty already uses "
    "for the model-vs-matched-naive comparison -- valid because all three policies score "
    "IDENTICAL candidate rows by construction (verified before any comparison is computed).",
    "This is one season (2025-26) only; no cross-season or prospectively frozen validation "
    "exists yet -- see the exploratory-comparison limitation above.",
    "A policy's out-of-sample MAE/RMSE advantage in this backtest is evidence for, but does "
    "not by itself constitute, a decision to change baseline_pipeline.py's production "
    "formula -- adopting any policy in production remains a separate, explicit step this "
    "script does not take, and should not be taken on this exploratory run's evidence alone.",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--revision", help="Exact Git revision of merged_gw.csv to pin")
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw/vaastav"))
    parser.add_argument("--evaluation-from-gw", type=int, default=3)
    parser.add_argument("--evaluation-to-gw", type=int, default=38)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--short-form-gameweeks", type=int, default=6)
    parser.add_argument("--defcon-short-form-gameweeks", type=int, default=10)
    parser.add_argument("--long-form-weight", type=float, default=0.8)
    parser.add_argument(
        "--minimum-calibration-gameweeks",
        type=int,
        default=DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS,
    )
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "docs/research/walk_forward_benchwarmers_2025_26_appearance_policy_backtest.json"
        ),
    )
    parser.add_argument(
        "--reference",
        type=Path,
        default=Path("docs/research/walk_forward_benchwarmers_2025_26.json"),
        help=(
            "Production backtest JSON the raw policy's aggregate metrics must match "
            "before any policy comparison is written."
        ),
    )
    return parser.parse_args()


def _archive_and_import(
    *, season: str, revision_sha: str | None, raw_root: Path, database_path: Path
):
    """Pin the newest (or requested) merged_gw.csv revision and import it.

    Duplicates the same small bootstrap every other backtest/calibration
    script in this codebase already has -- ``scripts/`` has no
    ``__init__.py`` and is not an installed package, so each CLI script
    stays independently runnable.
    """
    client = VaastavClient()
    revisions = client.file_revisions(season, filename="gws/merged_gw.csv")
    if not revisions:
        raise ValueError(f"no merged_gw.csv revisions found for {season}")
    if revision_sha:
        matches = [item for item in revisions if item.sha == revision_sha]
        if not matches:
            raise ValueError("requested revision did not modify merged_gw.csv")
        revision = matches[-1]
    else:
        revision = revisions[-1]

    archive_dir = raw_root / season / revision.sha
    players_path = archive_dir / "players_raw.csv"
    gameweeks_path = archive_dir / "merged_gw.csv"
    if players_path.is_file() and gameweeks_path.is_file():
        players = pd.read_csv(players_path)
        gameweeks = pd.read_csv(gameweeks_path)
    else:
        players = client.csv_at_revision(season, "players_raw.csv", revision)
        gameweeks = client.csv_at_revision(season, "gws/merged_gw.csv", revision)
        archive_dir.mkdir(parents=True, exist_ok=True)
        players.to_csv(players_path, index=False, lineterminator="\n")
        gameweeks.to_csv(gameweeks_path, index=False, lineterminator="\n")

    imported = import_player_fixture_history(
        players_path,
        gameweeks_path,
        season=season,
        source_revision=revision.sha,
        source_committed_at=revision.committed_at,
        database_path=database_path,
    )
    return imported, players, gameweeks


def _metrics_to_dict(metrics) -> dict[str, object]:
    return {
        "observations": metrics.observations,
        "mean_predicted_xpts": metrics.mean_predicted_xpts,
        "mean_actual_points": metrics.mean_actual_points,
        "mean_error": metrics.mean_error,
        "mean_absolute_error": metrics.mean_absolute_error,
        "root_mean_squared_error": metrics.root_mean_squared_error,
    }


def _bootstrap_to_dict(result) -> dict[str, object]:
    return {
        "point_estimate": result.point_estimate,
        "ci_low": result.ci_low,
        "ci_high": result.ci_high,
        "p_improvement_positive": result.p_improvement_positive,
        "resamples": result.resamples,
        "seed": result.seed,
        "cluster_unit": result.cluster_unit,
    }


def _comparison_verdict(mae: dict[str, object], rmse: dict[str, object]) -> str:
    """Classify a paired comparison as 'improves' / 'worsens' / 'mixed_or_inconclusive'.

    'improves' requires BOTH MAE and RMSE improvement CIs to lie entirely
    above zero (positive improvement means the first-named policy in the
    pair beats the second) -- mirroring
    paired_uncertainty.interpret_paired_verdict's own direction-aware
    three-state classification, applied here to a policy-vs-policy pair
    rather than model-vs-matched-naive.
    """
    mae_positive = mae["ci_low"] > 0.0
    rmse_positive = rmse["ci_low"] > 0.0
    mae_negative = mae["ci_high"] < 0.0
    rmse_negative = rmse["ci_high"] < 0.0
    if mae_positive and rmse_positive:
        return "improves"
    if mae_negative and rmse_negative:
        return "worsens"
    return "mixed_or_inconclusive"


def run(
    *,
    season: str,
    revision: str | None,
    raw_root: Path,
    evaluation_from_gw: int,
    evaluation_to_gw: int,
    database_path: Path,
    short_form_gameweeks: int,
    defcon_short_form_gameweeks: int,
    long_form_weight: float,
    minimum_calibration_gameweeks: int,
    resamples: int,
    seed: int,
    reference_path: Path,
) -> dict[str, object]:
    reference = load_reference(reference_path)

    imported, players_raw, gameweeks = _archive_and_import(
        season=season, revision_sha=revision, raw_root=raw_root, database_path=database_path
    )
    gameweeks_with_code = gameweeks.merge(
        players_raw.loc[:, ["id", "code"]], left_on="element", right_on="id", how="left"
    )

    with duckdb.connect(str(database_path)) as connection:
        bundle = materialize_appearance_policy_backtest(
            season=season,
            import_run_id=imported.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_with_code,
            players_raw_frame=players_raw,
            evaluation_from_gw=evaluation_from_gw,
            evaluation_to_gw=evaluation_to_gw,
            short_form_gameweeks=short_form_gameweeks,
            defcon_short_form_gameweeks=defcon_short_form_gameweeks,
            long_form_weight=long_form_weight,
            minimum_calibration_gameweeks=minimum_calibration_gameweeks,
        )
        # Obtained from the SAME inputs (connection, import_run_id,
        # gameweeks_frame, players_raw_frame, evaluation range, and every
        # other shared keyword argument) as the policy backtest above, so
        # verify_raw_row_level_parity below compares like-for-like.
        canonical = materialize_benchwarmers_walk_forward_backtest(
            season=season,
            import_run_id=imported.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_with_code,
            players_raw_frame=players_raw,
            evaluation_from_gw=evaluation_from_gw,
            evaluation_to_gw=evaluation_to_gw,
            short_form_gameweeks=short_form_gameweeks,
            defcon_short_form_gameweeks=defcon_short_form_gameweeks,
            long_form_weight=long_form_weight,
        )

    for policy in POLICIES:
        if not bundle.results_by_policy[policy].observations:
            raise ValueError(f"policy={policy!r} produced no scored observations")

    raw_observations = bundle.results_by_policy["raw"].observations
    raw_metrics = score_predictions(raw_observations)

    # The aggregate self-check (below) is NOT sufficient evidence the
    # duplicated materializer is identical -- aggregate metrics can match
    # even when individual rows disagree. This is the row-level check:
    # every observation field, gap key/flags, candidate-row count, and
    # evaluated-gameweek set is compared exactly against the canonical
    # materializer's own output over the same inputs. Raises BEFORE the
    # aggregate self_check and before anything is written -- a row-level
    # mismatch is a more specific, more actionable failure than an
    # aggregate-metric mismatch would be.
    raw_row_level_parity = verify_raw_row_level_parity(
        canonical,
        raw_observations,
        bundle.gaps,
        candidate_player_fixture_rows=bundle.candidate_player_fixture_rows,
        evaluated_gameweeks=bundle.evaluated_gameweeks,
    )
    if not raw_row_level_parity.matches:
        joined = "\n  - ".join(raw_row_level_parity.mismatches)
        raise ValueError(
            "raw row-level parity check failed: the policy backtest's raw policy diverges "
            f"from the canonical materializer at the individual-row level -- nothing was "
            f"written:\n  - {joined}"
        )

    # Raises ValueError (before any policy comparison is trusted) if this
    # script's own raw policy diverges from the committed reference -- both
    # this script's own self-check AND (together with the row-level parity
    # check above) confirmation that the duplicated walk-forward loop
    # faithfully reproduces the committed materializer.
    self_check = verify_self_check(
        reference=reference,
        reference_path=reference_path,
        import_run_id=imported.import_run_id,
        evaluation_from_gw=evaluation_from_gw,
        evaluation_to_gw=evaluation_to_gw,
        model_metrics=raw_metrics,
    )

    # All three policies must score IDENTICAL candidate rows -- verified
    # explicitly before any paired comparison is computed (build_paired_rows
    # itself would raise on a key-set mismatch, but checking here first
    # gives a clearer, policy-specific error message).
    key_sets = {
        policy: frozenset(
            (o.player_id, o.fixture_id, o.gameweek)
            for o in bundle.results_by_policy[policy].observations
        )
        for policy in POLICIES
    }
    if len(set(key_sets.values())) != 1:
        raise ValueError(
            "policies scored different candidate row sets -- gap eligibility must be "
            "policy-independent; this is a bug in materialize_appearance_policy_backtest"
        )

    policy_metrics = {
        policy: _metrics_to_dict(score_predictions(bundle.results_by_policy[policy].observations))
        for policy in POLICIES
    }

    comparisons: dict[str, dict[str, object]] = {}
    for focus_policy, comparator_policy in POLICY_COMPARISONS:
        paired_rows = build_paired_rows(
            bundle.results_by_policy[focus_policy].observations,
            bundle.results_by_policy[comparator_policy].observations,
        )
        mae_result, rmse_result = cluster_bootstrap(
            paired_rows,
            cluster_key=lambda row: row.gameweek,
            cluster_label="gameweek",
            resamples=resamples,
            seed=seed,
        )
        mae_dict = _bootstrap_to_dict(mae_result)
        rmse_dict = _bootstrap_to_dict(rmse_result)
        comparisons[f"{focus_policy}_vs_{comparator_policy}"] = {
            "focus_policy": focus_policy,
            "comparator_policy": comparator_policy,
            "paired_rows": len(paired_rows),
            "mae_improvement": mae_dict,
            "rmse_improvement": rmse_dict,
            "verdict": _comparison_verdict(mae_dict, rmse_dict),
        }

    eligible_fit_gameweeks = {
        policy: sum(
            1 for _, fit in bundle.results_by_policy[policy].fit_trajectory if fit is not None
        )
        for policy in ("global", "high_end_shrinkage")
    }

    global_vs_raw = comparisons["global_vs_raw"]["verdict"]
    shrink_vs_raw = comparisons["high_end_shrinkage_vs_raw"]["verdict"]
    shrink_vs_global_result = comparisons["high_end_shrinkage_vs_global"]
    shrink_vs_global = shrink_vs_global_result["verdict"]
    shrink_vs_global_mae = shrink_vs_global_result["mae_improvement"]
    shrink_vs_global_rmse = shrink_vs_global_result["rmse_improvement"]
    # A one-sided signal (either metric's CI entirely below zero, even if
    # the OTHER metric's CI straddles zero and keeps the paired VERDICT at
    # "mixed_or_inconclusive") is still real evidence shrinkage is not
    # simply as-good-as global on at least one axis -- checked explicitly
    # here rather than only trusting the two-metrics-must-agree verdict,
    # which would otherwise treat "MAE reliably worse, RMSE ambiguous" the
    # same as "both metrics genuinely ambiguous".
    shrink_worse_than_global_on_at_least_one_metric = (
        shrink_vs_global_mae["ci_high"] < 0.0 or shrink_vs_global_rmse["ci_high"] < 0.0
    )

    exploratory_caveat = (
        "This verdict is better-supported WITHIN this exploratory, same-season 2025-26 "
        "comparison only -- the decision to test high_end_shrinkage at all was informed by "
        "diagnose_appearance_segments.py's own 2025-26 findings, then evaluated on that SAME "
        "season again, and this script's bootstrap CIs do not account for that policy-"
        "selection uncertainty. An independent season or a prospectively frozen 2026-27 "
        "evaluation is required before production adoption."
    )

    if (
        shrink_vs_raw == "improves"
        and shrink_vs_global != "worsens"
        and not shrink_worse_than_global_on_at_least_one_metric
    ):
        recommendation = (
            f"high_end_shrinkage improves over raw (both MAE and RMSE improvement CIs above "
            f"zero, {comparisons['high_end_shrinkage_vs_raw']['paired_rows']} paired rows), "
            f"and does not show a one-sided reliable loss against global on either metric "
            f"(high_end_shrinkage_vs_global verdict: {shrink_vs_global}). global_vs_raw "
            f"verdict: {global_vs_raw}. This out-of-sample result is the first evidence, "
            f"within this exploratory comparison, supporting a production calibration "
            f"change. {exploratory_caveat}"
        )
    elif global_vs_raw == "improves":
        one_sided_note = (
            " (its paired verdict is 'mixed_or_inconclusive' under the two-metrics-must-agree "
            "rule, but at least one metric's own CI lies entirely below zero -- a one-sided "
            "signal that shrinkage is not simply as-good-as global, even though it is not a "
            "two-sided 'worsens' verdict)"
            if shrink_vs_global == "mixed_or_inconclusive" and shrink_worse_than_global_on_at_least_one_metric
            else ""
        )
        recommendation = (
            f"global improves over raw (both MAE and RMSE improvement CIs above zero, "
            f"{comparisons['global_vs_raw']['paired_rows']} paired rows). "
            f"high_end_shrinkage_vs_raw is {shrink_vs_raw} but high_end_shrinkage_vs_global "
            f"is {shrink_vs_global}{one_sided_note} -- global calibration, not high-end-only "
            f"shrinkage, is the better-supported result within this exploratory comparison. "
            f"{exploratory_caveat}"
        )
    else:
        recommendation = (
            f"Neither global nor high_end_shrinkage shows a stable, paired out-of-sample "
            f"MAE+RMSE improvement over raw this run (global_vs_raw: {global_vs_raw}, "
            f"high_end_shrinkage_vs_raw: {shrink_vs_raw}) -- this exploratory backtest does "
            f"NOT support changing production calibration on this evidence alone."
        )

    return {
        "$schema_note": (
            "Causal WALK-FORWARD EXECUTION (not an independent confirmatory backtest) of "
            "appearance start_probability calibration policies (raw/global/"
            "high_end_shrinkage) chosen from an EXPLORATORY, SAME-SEASON (2025-26) policy "
            "specification -- see exploratory_comparison_note. Each policy's predicted_xpts "
            "is recomputed from a policy-specific AppearanceProjection (an OLS transform of "
            "start_probability plus a proportional rescale of its dependent fields, never a "
            "pure start_probability substitution) and re-scored through the UNCHANGED "
            "project_benchwarmers_*/weight_* component chain. Every policy's threshold/fit "
            "is derived ONLY from strictly-prior, deadline-safe gameweeks at each "
            "walk-forward step -- so no future OUTCOME ever enters an earlier fit -- but the "
            "DECISION to test these specific policies was informed by "
            "diagnose_appearance_segments.py's own 2025-26 findings, then evaluated on that "
            "SAME season again; this script's bootstrap CIs do not account for that "
            "selection uncertainty. Pairs with "
            "docs/research/walk_forward_benchwarmers_2025_26_appearance_segments.json "
            "(the read-only diagnostic that motivated testing these policies) and "
            "docs/research/walk_forward_benchwarmers_2025_26.json (raw policy's own "
            "self-check reference). Read-only measurement; no production formula changed."
        ),
        "season": season,
        "import_run_id": imported.import_run_id,
        "evaluation_from_gw": evaluation_from_gw,
        "evaluation_to_gw": evaluation_to_gw,
        "minimum_calibration_gameweeks": minimum_calibration_gameweeks,
        "high_end_start_probability_threshold": HIGH_END_START_PROBABILITY_THRESHOLD,
        "self_check": self_check,
        "raw_row_level_parity": {
            "matches": raw_row_level_parity.matches,
            "canonical_rows": raw_row_level_parity.canonical_rows,
            "policy_backtest_rows": raw_row_level_parity.policy_backtest_rows,
            "note": (
                "Row-by-row comparison of this run's raw policy against "
                "materialize_benchwarmers_walk_forward_backtest's own canonical output over "
                "the same inputs -- EVERY observation field (season, actual_points, "
                "predicted_xpts, deadline, fixture_kickoff, feature_cutoff, "
                "outcome_available_at), EVERY gap field (team, position, flags -- not flags "
                "alone), candidate_player_fixture_rows, and evaluated_gameweeks. Duplicate "
                "observation/gap keys on either side are detected explicitly before any "
                "dict-keyed comparison (a dict construction would otherwise silently collapse "
                "a duplicate key, hiding a real bug behind an apparent match). This is the "
                "row-level evidence the aggregate self_check above cannot provide by itself "
                "(aggregate metrics can match even when individual rows disagree). Only "
                "matches=true licenses describing raw as having EXACT FIELD-LEVEL PARITY with "
                "the canonical materializer's dataclass output (not 'byte-identical' -- that "
                "term is reserved for actual serialized bytes/files, e.g. this run's own JSON "
                "output on a determinism rerun); this script raises before writing anything if "
                "it were false, so a written result always has matches=true."
            ),
        },
        "candidate_player_fixture_rows": bundle.candidate_player_fixture_rows,
        "scored_player_fixture_rows": raw_metrics.observations,
        "eligible_fit_gameweeks": eligible_fit_gameweeks,
        "evaluated_gameweeks": len(bundle.evaluated_gameweeks),
        "policy_metrics": policy_metrics,
        "comparisons": comparisons,
        "recommendation": recommendation,
        "exploratory_comparison_note": exploratory_caveat,
        "expected_minutes_out_of_scope_note": EXPECTED_MINUTES_OUT_OF_SCOPE_NOTE,
        "methodology_notes": [
            "All three policies (raw/global/high_end_shrinkage) are materialized from ONE "
            "shared walk-forward pass over the same candidate rows -- policy-independent "
            "upstream queries (team strength, player rates, appearance history, "
            "league-average bonus rates) are computed once per gameweek; only the "
            "AppearanceProjection substitution and the downstream weight_*/"
            "compose_baseline_projection calls are repeated per policy.",
            "raw uses the raw, unmodified AppearanceProjection -- confirmed to have EXACT "
            "FIELD-LEVEL PARITY (every observation and gap field, plus every count) with "
            "validation.benchwarmers_backtest's own committed materializer by BOTH this run's "
            "aggregate self_check against --reference AND its row-level raw_row_level_parity "
            "check -- the aggregate check alone cannot rule out two different row-level "
            "results sharing the same aggregate mean/sum-of-squares.",
            "global applies the causal walk-forward start_probability calibration "
            "(fit_for_gameweek, refit every step on strictly-prior, DEADLINE-SAFE gameweeks "
            "only -- row.gameweek < target AND row.outcome_available_at <= target_deadline, "
            "so a postponed fixture's outcome cannot enter a fit before it was actually "
            "known) to every row; high_end_shrinkage applies that SAME fit only to rows "
            "whose raw start_probability >= high_end_start_probability_threshold, else raw.",
            "A gameweek with no eligible prior-gameweeks-only fit (see eligible_fit_gameweeks) "
            "scores global/high_end_shrinkage identically to raw for that gameweek's rows -- "
            "never a fabricated calibration.",
            "Comparisons reuse validation.paired_uncertainty.build_paired_rows/"
            "cluster_bootstrap -- the SAME gameweek-cluster percentile bootstrap the "
            "model-vs-matched-naive comparator already uses -- valid here because all three "
            "policies score IDENTICAL candidate rows by construction (verified before any "
            "comparison is computed).",
            "verdict is direction-aware (mirrors "
            "paired_uncertainty.interpret_paired_verdict's own three-state classification): "
            "'improves' requires BOTH MAE and RMSE improvement CIs entirely above zero, "
            "'worsens' requires both entirely below zero, anything else is "
            "'mixed_or_inconclusive'. This verdict describes THIS exploratory comparison "
            "only -- see exploratory_comparison_note; it is not an independently confirmed "
            "result.",
        ],
        "limitations": LIMITATIONS,
        "reproduce": (
            ".venv\\Scripts\\python.exe scripts/backtest_appearance_calibration_policies.py "
            "--season 2025-26"
        ),
    }


def main() -> None:
    args = parse_args()
    result = run(
        season=args.season,
        revision=args.revision,
        raw_root=args.raw_root,
        evaluation_from_gw=args.evaluation_from_gw,
        evaluation_to_gw=args.evaluation_to_gw,
        database_path=args.database,
        short_form_gameweeks=args.short_form_gameweeks,
        defcon_short_form_gameweeks=args.defcon_short_form_gameweeks,
        long_form_weight=args.long_form_weight,
        minimum_calibration_gameweeks=args.minimum_calibration_gameweeks,
        resamples=args.resamples,
        seed=args.seed,
        reference_path=args.reference,
    )
    output = json.dumps(result, indent=2, sort_keys=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output + "\n", encoding="utf-8")

    parity = result["raw_row_level_parity"]
    print(
        f"raw_row_level_parity: matches={parity['matches']} "
        f"canonical_rows={parity['canonical_rows']} policy_backtest_rows={parity['policy_backtest_rows']}"
    )
    print(f"self_check against {args.reference}: {result['self_check']['status']}")
    print(
        f"candidate_rows={result['candidate_player_fixture_rows']} "
        f"scored_rows={result['scored_player_fixture_rows']} "
        f"eligible_fit_gameweeks={result['eligible_fit_gameweeks']}"
    )
    print()
    for policy, metrics in result["policy_metrics"].items():
        print(
            f"{policy}: mae={metrics['mean_absolute_error']:.4f} "
            f"rmse={metrics['root_mean_squared_error']:.4f} "
            f"mean_error={metrics['mean_error']:+.4f}"
        )
    print()
    for label, comparison in result["comparisons"].items():
        mae = comparison["mae_improvement"]
        rmse = comparison["rmse_improvement"]
        print(
            f"{label}: verdict={comparison['verdict']} "
            f"mae_improvement={mae['point_estimate']:+.4f} ci=[{mae['ci_low']:.4f}, {mae['ci_high']:.4f}] "
            f"rmse_improvement={rmse['point_estimate']:+.4f} ci=[{rmse['ci_low']:.4f}, {rmse['ci_high']:.4f}]"
        )
    print()
    print(result["recommendation"])
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
