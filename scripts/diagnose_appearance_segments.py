"""Segment the appearance model's own walk-forward calibration to locate bias.

``scripts/assess_appearance_calibration.py`` found the appearance model's
pooled walk-forward slope for both ``start_probability`` and
``expected_minutes`` below 1.0 in the primary ``appearance_eligible`` cohort
-- systematic overconfidence in aggregate. That finding is a single number
per target per cohort; it cannot say WHERE the miscalibration concentrates.
This is a read-only diagnostic over the same already-scored
``AppearanceObservation``/``BacktestObservation`` outputs
(``materialize_benchwarmers_walk_forward_backtest``, the same function every
other backtest/calibration script in this codebase calls) that answers that
question with plain per-segment descriptive statistics -- mean bias,
Brier/MSE/MAE, a gameweek-cluster bootstrap CI -- never a per-segment
regression slope (see ``validation.appearance_segments`` module docstring for
why: several required segments have too little predictor variance to support
a stable slope).

Four cohorts (``validation.appearance_segments.COHORTS``):

- ``appearance_eligible`` (primary)
- ``xpts_scored_aligned`` (sensitivity -- exactly
  ``assess_appearance_calibration.py``'s own sensitivity cohort; spans every
  evaluated gameweek, so it must NOT be used as the comparator for a
  high-band claim -- see ``xpts_same_window_aligned`` below)
- ``xpts_high_band_aligned`` (new sensitivity cohort -- within
  ``xpts_scored_aligned``, restricted to keys that were themselves members of
  ``validation.walk_forward_calibration``'s own out-of-sample, prior-only
  75th-percentile high-predicted-xPts band. This is the exact cohort the
  committed xPts calibration script (``scripts/assess_backtest_calibration.py``)
  found to be materially overconfident -- checking whether appearance-model
  average overprediction bias is ALSO concentrated there is the central
  question this script exists to answer.)
- ``xpts_same_window_aligned`` (new sensitivity cohort, the correct
  comparator for high-band claims -- within ``xpts_scored_aligned``,
  restricted to keys from the SAME ``walk_forward_calibration`` call's
  ``overall_evaluation_rows``, i.e. the exact same eligible-gameweek window
  ``xpts_high_band_aligned`` was drawn from, never inferred from its own
  min/max gameweek.)

Four segment axes, all fixed (not population-adaptive) boundaries -- see
``validation.appearance_segments`` for why fixed bands are required here and
exactly where each boundary comes from: ``start_probability`` band,
``expected_minutes`` band, ``position``, and ``gameweek_phase``.

This script measures and reports only. It calls no
``project_benchwarmers_*``/``weight_*`` component function differently,
introduces no second scoring path, and applies no calibration to any
production projection, appearance formula, xPts component, or the optimizer.

Before any segment work begins, this run's recomputed aggregate backtest
metrics are compared against ``--reference`` (defaulting to the committed
``docs/research/walk_forward_benchwarmers_2025_26.json``) via
``validation.backtest_self_check.verify_self_check``. A mismatch raises
``ValueError`` and nothing is written.
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
from fpl_model.validation.appearance_calibration import (
    AppearanceCalibrationTarget,
    xpts_keys_from_backtest_observations,
    xpts_scored_aligned_observations,
)
from fpl_model.validation.appearance_segments import (
    COHORTS,
    GAMEWEEK_PHASE_LATE_START_GW,
    GAMEWEEK_PHASE_MID_START_GW,
    MINIMUM_GAMEWEEK_CLUSTERS_FOR_STABLE_LABEL,
    CohortName,
    SegmentSummary,
    group_rows,
    observations_to_segment_rows,
    paired_contrast_bootstrap,
    summarize_segment,
    validate_segment_partition,
    xpts_high_band_aligned_observations,
    xpts_high_band_and_same_window_keys_from_backtest_observations,
    xpts_same_window_aligned_observations,
)
from fpl_model.validation.backtest import score_predictions
from fpl_model.validation.backtest_self_check import load_reference, verify_self_check
from fpl_model.validation.benchwarmers_backtest import (
    AppearanceObservation,
    materialize_benchwarmers_walk_forward_backtest,
)
from fpl_model.validation.paired_uncertainty import DEFAULT_RESAMPLES, DEFAULT_SEED
from fpl_model.validation.walk_forward_calibration import (
    DEFAULT_HIGH_BAND_PERCENTILE,
)
from fpl_model.validation.walk_forward_calibration import (
    DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS as DEFAULT_XPTS_MINIMUM_CALIBRATION_GAMEWEEKS,
)

TARGETS: tuple[AppearanceCalibrationTarget, ...] = ("start_probability", "expected_minutes")
SEGMENT_AXES = (
    "start_probability_band",
    "expected_minutes_band",
    "position",
    "gameweek_phase",
)

LIMITATIONS = [
    "This is a descriptive segment diagnostic, not a second calibration: no per-segment "
    "regression slope is ever fit (see validation.appearance_segments module docstring) -- "
    "several required segments (e.g. the top start_probability/expected_minutes band, or a "
    "thin gameweek_phase x position cell) have too little predictor variance to support a "
    "stable slope. insufficient_variation flags exactly when a segment's own predicted "
    "values have fewer than 2 rows or zero variance.",
    "Terminology: mean(predicted - actual) measures average over/under-prediction bias, "
    "not model confidence or prediction extremeness -- 'overconfidence' is the correct word "
    "for the pooled walk-forward slope in assess_appearance_calibration.py's own output "
    "(slope < 1.0), not for this script's segmented mean bias. See answers.terminology_note.",
    "A 'concentrated'/'larger' claim requires the PAIRED gameweek-cluster CONTRAST CI "
    "(focus_bias - comparator_bias, one shared cluster-label draw per replicate) to lie "
    "entirely above zero -- a focus side's own individual CI excluding zero is necessary but "
    "NOT sufficient, since both sides can be individually significant and same-signed while "
    "their difference's own sampling uncertainty still crosses zero. See "
    "answers.contrast_methodology_note and validation.appearance_segments."
    "paired_contrast_bootstrap's own docstring.",
    "Association, not causation: a segment with a large mean_bias shows the appearance "
    "model's OWN prediction is biased in that segment. It does not, by itself, prove that "
    "appearance-model bias -- as opposed to the per-90 rate components built on top of it, "
    "which are highly correlated with start_probability/expected_minutes in exactly the "
    "high bands this script also segments -- is the cause of any particular amount of total "
    "xPts error in that segment. See assess_appearance_calibration.py's own isolation "
    "argument for why the appearance model can be assessed on its own terms, but that does "
    "not extend to a causal share-of-total-error claim.",
    "xpts_high_band_aligned and xpts_same_window_aligned membership are both derived by "
    "calling validation.walk_forward_calibration.walk_forward_calibration directly (not a "
    "re-derived percentile or an inferred min/max-gameweek window) specifically so they can "
    "never silently drift from the committed xPts calibration's own rows, and so the two "
    "cohorts are guaranteed to share the same eligible-gameweek window for a valid "
    "high-band-vs-comparator claim -- see validation.appearance_segments module docstring.",
    "gameweek_phase boundaries (GW1-13/14-26/27-38) are a new, fixed diagnostic convention "
    "documented in validation.appearance_segments' own module docstring -- they were not "
    "chosen to optimise any diagnostic result and do not correspond to a claim about when "
    "appearance miscalibration itself changes. Over the default GW3-38 evaluation range "
    "this yields 11/13/12 observed gameweeks per phase; 'stable' wording is withheld below "
    "MINIMUM_GAMEWEEK_CLUSTERS_FOR_STABLE_LABEL distinct gameweeks regardless of the CI, "
    "since a percentile bootstrap from very few clusters can exclude zero by chance alone.",
    "start_probability/expected_minutes bands are fixed absolute thresholds, not "
    "population-adaptive quantiles -- a band's meaning is therefore stable across cohorts, "
    "reruns, and (if ever extended) seasons, unlike diagnose_backtest_segments.py's "
    "unrelated quartile-based xPts-band segmentation.",
    "This is one season (2025-26) only; no cross-season validation exists yet, and this "
    "diagnostic has not compared a global calibration policy against a high-end-only policy "
    "out of sample -- it identifies WHERE bias concentrates, not which correction policy "
    "would perform better on held-out data. See answers.correction_recommendation.",
    "Read-only measurement: no calibration, shrinkage, or formula change is applied to "
    "baseline_pipeline.py, benchwarmers_backtest.py, or the optimizer as a result of this "
    "script.",
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
        "--xpts-minimum-calibration-gameweeks",
        type=int,
        default=DEFAULT_XPTS_MINIMUM_CALIBRATION_GAMEWEEKS,
        help="Passed through to walk_forward_calibration for xpts_high_band_aligned membership.",
    )
    parser.add_argument(
        "--high-band-percentile", type=float, default=DEFAULT_HIGH_BAND_PERCENTILE
    )
    parser.add_argument("--resamples", type=int, default=DEFAULT_RESAMPLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/research/walk_forward_benchwarmers_2025_26_appearance_segments.json"),
    )
    parser.add_argument(
        "--reference",
        type=Path,
        default=Path("docs/research/walk_forward_benchwarmers_2025_26.json"),
        help=(
            "Production backtest JSON this run's aggregate metrics must match "
            "before any segment output is written."
        ),
    )
    return parser.parse_args()


def _archive_and_import(
    *,
    season: str,
    revision_sha: str | None,
    raw_root: Path,
    database_path: Path,
):
    """Pin the newest (or requested) merged_gw.csv revision and import it.

    Duplicates the same small bootstrap every other backtest/calibration
    script in this codebase already has, rather than importing a private
    helper across script files -- ``scripts/`` has no ``__init__.py`` and is
    not an installed package, so each CLI script stays independently
    runnable.
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


def _ci_excludes_zero(bootstrap: dict[str, object]) -> bool:
    return bootstrap["ci_low"] > 0.0 or bootstrap["ci_high"] < 0.0


def _stability_label(distinct_gameweeks: int, bootstrap: dict[str, object]) -> str:
    """Return 'stable, excludes zero' / 'crosses zero' / a thin-cluster caveat.

    A percentile bootstrap CI built from very few gameweek clusters can
    exclude zero by chance alone -- withhold the "stable" claim entirely
    below MINIMUM_GAMEWEEK_CLUSTERS_FOR_STABLE_LABEL, regardless of what the
    CI itself shows, rather than reporting a confident-sounding label a thin
    sample cannot support.
    """
    if distinct_gameweeks < MINIMUM_GAMEWEEK_CLUSTERS_FOR_STABLE_LABEL:
        return (
            f"CI excludes zero but only {distinct_gameweeks} gameweek cluster(s) support it "
            f"(< {MINIMUM_GAMEWEEK_CLUSTERS_FOR_STABLE_LABEL} minimum) -- not labelled stable"
            if _ci_excludes_zero(bootstrap)
            else f"crosses zero ({distinct_gameweeks} gameweek cluster(s))"
        )
    return "stable, excludes zero" if _ci_excludes_zero(bootstrap) else "crosses zero"


def _summary_to_dict(
    summary: SegmentSummary, *, parent_bias_sum: float | None = None
) -> dict[str, object]:
    share_of_net_aggregate_bias = (
        summary.bias_sum / parent_bias_sum
        if parent_bias_sum is not None and parent_bias_sum != 0.0
        else None
    )
    bootstrap_dict = {
        "point_estimate": summary.mean_bias_bootstrap.point_estimate,
        "ci_low": summary.mean_bias_bootstrap.ci_low,
        "ci_high": summary.mean_bias_bootstrap.ci_high,
        "resamples": summary.mean_bias_bootstrap.resamples,
        "seed": summary.mean_bias_bootstrap.seed,
    }
    return {
        "rows": summary.rows,
        "distinct_gameweeks": summary.distinct_gameweeks,
        "mean_predicted": summary.mean_predicted,
        "mean_actual": summary.mean_actual,
        "mean_bias": summary.mean_bias,
        "bias_sum": summary.bias_sum,
        "share_of_net_aggregate_bias": share_of_net_aggregate_bias,
        "mean_bias_bootstrap": bootstrap_dict,
        "stability_label": _stability_label(summary.distinct_gameweeks, bootstrap_dict),
        "insufficient_variation": summary.insufficient_variation,
        "predicted_variance": summary.predicted_variance,
        "brier_score": summary.brier_score,
        "observed_start_rate": summary.observed_start_rate,
        "mse": summary.mse,
        "mae": summary.mae,
    }


def _segment_axis_table(
    rows: tuple, *, axis: str, target: str, resamples: int, seed: int, parent_bias_sum: float
) -> dict[str, object]:
    groups = group_rows(rows, by=axis)
    validate_segment_partition(rows, groups)
    return {
        key: _summary_to_dict(
            summarize_segment(group, target=target, resamples=resamples, seed=seed),
            parent_bias_sum=parent_bias_sum,
        )
        for key, group in groups.items()
    }


def _coverage_note(cohort_rows: dict[CohortName, int]) -> dict[str, object]:
    return {
        "appearance_eligible_rows": cohort_rows["appearance_eligible"],
        "xpts_scored_aligned_rows": cohort_rows["xpts_scored_aligned"],
        "xpts_high_band_aligned_rows": cohort_rows["xpts_high_band_aligned"],
        "xpts_same_window_aligned_rows": cohort_rows["xpts_same_window_aligned"],
    }


def _contrast_verdict(contrast) -> str:
    """Classify a ``ContrastBootstrap`` into 'above_zero' / 'below_zero' / 'crosses_zero'.

    This is the ONLY basis this script uses for a "concentrated"/"larger"
    claim -- never a focus side's own CI excluding zero (both sides can be
    individually significant and same-signed while their difference's own
    sampling uncertainty still crosses zero; see
    validation.appearance_segments module docstring). A thin shared-cluster
    count (below MINIMUM_GAMEWEEK_CLUSTERS_FOR_STABLE_LABEL) additionally
    downgrades an "above_zero"/"below_zero" verdict to a thin-cluster
    caveat, mirroring _stability_label's own single-sided treatment.
    """
    if contrast.shared_distinct_gameweeks < MINIMUM_GAMEWEEK_CLUSTERS_FOR_STABLE_LABEL:
        return "thin_clusters"
    if contrast.ci_low > 0.0:
        return "above_zero"
    if contrast.ci_high < 0.0:
        return "below_zero"
    return "crosses_zero"


def _contrast_answer(
    *,
    focus_rows,
    comparator_rows,
    focus_label: str,
    comparator_label: str,
    resamples: int,
    seed: int,
) -> dict[str, object]:
    """Run the paired gameweek-cluster contrast bootstrap and format its answer.

    Returns a dict with the focus/comparator bias, the contrast point
    estimate and CI, the shared distinct-gameweek count, and a
    'concentrated' boolean that is True if-and-only-if the CONTRAST's own
    CI lies entirely above zero (never merely because the focus side's own
    CI excludes zero) -- see ``_contrast_verdict``.
    """
    contrast = paired_contrast_bootstrap(
        focus_rows, comparator_rows, resamples=resamples, seed=seed
    )
    verdict = _contrast_verdict(contrast)
    concentrated = verdict == "above_zero"

    if verdict == "above_zero":
        narrative = (
            f"{focus_label} bias ({contrast.focus_bias:+.4f}) is reliably larger than "
            f"{comparator_label} bias ({contrast.comparator_bias:+.4f}): the paired contrast "
            f"CI [{contrast.ci_low:.4f}, {contrast.ci_high:.4f}] lies entirely above zero"
        )
    elif verdict == "below_zero":
        narrative = (
            f"{focus_label} bias ({contrast.focus_bias:+.4f}) is reliably SMALLER than "
            f"{comparator_label} bias ({contrast.comparator_bias:+.4f}): the paired contrast "
            f"CI [{contrast.ci_low:.4f}, {contrast.ci_high:.4f}] lies entirely below zero"
        )
    elif verdict == "thin_clusters":
        narrative = (
            f"{focus_label} bias ({contrast.focus_bias:+.4f}) vs {comparator_label} bias "
            f"({contrast.comparator_bias:+.4f}): contrast point estimate "
            f"{contrast.contrast_point_estimate:+.4f}, CI [{contrast.ci_low:.4f}, "
            f"{contrast.ci_high:.4f}] -- only {contrast.shared_distinct_gameweeks} shared "
            f"gameweek cluster(s) support it (< {MINIMUM_GAMEWEEK_CLUSTERS_FOR_STABLE_LABEL} "
            "minimum), not labelled a stable signal either way"
        )
    else:
        narrative = (
            f"{focus_label} bias ({contrast.focus_bias:+.4f}) point estimate exceeds "
            f"{comparator_label} bias ({contrast.comparator_bias:+.4f}), but the paired "
            f"contrast CI [{contrast.ci_low:.4f}, {contrast.ci_high:.4f}] crosses zero -- "
            "NOT a reliable difference at this sample size, even though one or both sides' "
            "own individual CIs may exclude zero"
        )

    return {
        "focus_bias": contrast.focus_bias,
        "comparator_bias": contrast.comparator_bias,
        "contrast_point_estimate": contrast.contrast_point_estimate,
        "contrast_bootstrap": {
            "ci_low": contrast.ci_low,
            "ci_high": contrast.ci_high,
            "resamples": contrast.resamples,
            "seed": contrast.seed,
        },
        "shared_distinct_gameweeks": contrast.shared_distinct_gameweeks,
        "verdict": verdict,
        "concentrated": concentrated,
        "narrative": narrative,
    }


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
    xpts_minimum_calibration_gameweeks: int,
    high_band_percentile: float,
    resamples: int,
    seed: int,
    reference_path: Path,
) -> dict[str, object]:
    reference = load_reference(reference_path)

    imported, players_raw, gameweeks = _archive_and_import(
        season=season,
        revision_sha=revision,
        raw_root=raw_root,
        database_path=database_path,
    )
    gameweeks_with_code = gameweeks.merge(
        players_raw.loc[:, ["id", "code"]],
        left_on="element",
        right_on="id",
        how="left",
    )

    with duckdb.connect(str(database_path)) as connection:
        backtest = materialize_benchwarmers_walk_forward_backtest(
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

    if not backtest.observations:
        raise ValueError("backtest produced no scored observations to diagnose")
    if not backtest.appearance_observations:
        raise ValueError("backtest produced no appearance observations to diagnose")

    model_metrics = score_predictions(backtest.observations)

    # Raises ValueError (before any segment work) if this run's recomputed
    # aggregate metrics diverge from the reference production backtest.
    self_check = verify_self_check(
        reference=reference,
        reference_path=reference_path,
        import_run_id=imported.import_run_id,
        evaluation_from_gw=evaluation_from_gw,
        evaluation_to_gw=evaluation_to_gw,
        model_metrics=model_metrics,
    )

    xpts_keys = xpts_keys_from_backtest_observations(backtest.observations)
    xpts_scored_aligned = xpts_scored_aligned_observations(
        backtest.appearance_observations, xpts_keys
    )
    # Both key sets are derived from the SAME walk_forward_calibration call,
    # so they share the same eligible-gameweek window by construction -- see
    # xpts_high_band_and_same_window_keys_from_backtest_observations's own
    # docstring for why this must not be two separately-derived windows.
    high_band_keys, same_window_keys = xpts_high_band_and_same_window_keys_from_backtest_observations(
        backtest.observations,
        minimum_calibration_gameweeks=xpts_minimum_calibration_gameweeks,
        high_band_percentile=high_band_percentile,
    )
    xpts_high_band_aligned = xpts_high_band_aligned_observations(
        xpts_scored_aligned, high_band_keys
    )
    xpts_same_window_aligned = xpts_same_window_aligned_observations(
        xpts_scored_aligned, same_window_keys
    )

    cohort_observations: dict[CohortName, tuple[AppearanceObservation, ...]] = {
        "appearance_eligible": backtest.appearance_observations,
        "xpts_scored_aligned": xpts_scored_aligned,
        "xpts_high_band_aligned": xpts_high_band_aligned,
        "xpts_same_window_aligned": xpts_same_window_aligned,
    }
    for cohort in COHORTS:
        if not cohort_observations[cohort]:
            raise ValueError(f"cohort={cohort!r} has zero rows; cannot summarize")

    cohort_rows_count: dict[CohortName, int] = {
        cohort: len(cohort_observations[cohort]) for cohort in COHORTS
    }

    cohort_results: dict[str, dict[str, object]] = {}
    # Raw rows are kept alongside the serialized summaries (never re-derived
    # from the dicts) specifically so paired_contrast_bootstrap can be
    # called directly on the true row sets below -- a contrast bootstrap
    # needs each side's own per-gameweek sufficient statistics, which the
    # serialized SegmentSummary dicts do not carry.
    cohort_rows_by_target: dict[tuple[str, str], tuple] = {}
    for cohort in COHORTS:
        observations = cohort_observations[cohort]
        target_results: dict[str, dict[str, object]] = {}
        for target in TARGETS:
            rows = observations_to_segment_rows(observations, target=target)
            cohort_rows_by_target[(cohort, target)] = rows
            overall_summary = summarize_segment(rows, target=target, resamples=resamples, seed=seed)
            segment_tables = {
                axis: _segment_axis_table(
                    rows,
                    axis=axis,
                    target=target,
                    resamples=resamples,
                    seed=seed,
                    parent_bias_sum=overall_summary.bias_sum,
                )
                for axis in SEGMENT_AXES
            }
            target_results[target] = {
                "overall": _summary_to_dict(overall_summary),
                **{f"by_{axis}": table for axis, table in segment_tables.items()},
            }
        cohort_results[cohort] = target_results

    coverage = _coverage_note(cohort_rows_count)

    # --- Direct answers -----------------------------------------------
    primary = cohort_results["appearance_eligible"]

    def _top_band_key(target: str) -> tuple[str, str]:
        if target == "start_probability":
            return "start_probability_band", "[0.8,1.0]"
        return "expected_minutes_band", "[75,90]"

    def _top_band_rows(target: str):
        axis, band = _top_band_key(target)
        groups = group_rows(cohort_rows_by_target[("appearance_eligible", target)], by=axis)
        return groups.get(band)

    # Contrast 2a: top prediction bands vs the primary cohort's own overall
    # rows (start_probability [0.8,1.0] and expected_minutes [75,90] vs
    # appearance_eligible overall).
    top_band_contrasts: dict[str, dict[str, object]] = {}
    for target in TARGETS:
        focus_rows = _top_band_rows(target)
        _, band_label = _top_band_key(target)
        if focus_rows is None:
            top_band_contrasts[target] = {
                "focus_bias": None,
                "comparator_bias": None,
                "contrast_point_estimate": None,
                "contrast_bootstrap": None,
                "shared_distinct_gameweeks": None,
                "verdict": "no_data",
                "concentrated": False,
                "narrative": "insufficient data (no rows in the top band)",
            }
            continue
        top_band_contrasts[target] = _contrast_answer(
            focus_rows=focus_rows,
            comparator_rows=cohort_rows_by_target[("appearance_eligible", target)],
            focus_label=f"{band_label} band",
            comparator_label="appearance_eligible overall",
            resamples=resamples,
            seed=seed,
        )

    # Contrast 2b: xPts high band vs the same-window comparator -- both
    # sides drawn from the SAME walk_forward_calibration eligible-gameweek
    # window (see xpts_high_band_and_same_window_keys_from_backtest_observations).
    xpts_high_band_contrasts: dict[str, dict[str, object]] = {
        target: _contrast_answer(
            focus_rows=cohort_rows_by_target[("xpts_high_band_aligned", target)],
            comparator_rows=cohort_rows_by_target[("xpts_same_window_aligned", target)],
            focus_label="xpts_high_band_aligned",
            comparator_label="xpts_same_window_aligned",
            resamples=resamples,
            seed=seed,
        )
        for target in TARGETS
    }

    def _position_ranking(target: str) -> str:
        # Ranked by bias_sum magnitude (sum(predicted - actual) = mean_bias *
        # rows), NOT abs(mean_bias) alone: bias_sum is additive across the
        # partition (sums exactly to the cohort's own bias_sum), so it is
        # the mathematically real "contribution to the aggregate bias"
        # quantity -- a position with a large per-row mean_bias but few rows
        # contributes less to the total than mean_bias alone would suggest,
        # and a position whose bias_sum has the OPPOSITE sign from the
        # cohort's own aggregate bias_sum is OFFSETTING it, not contributing
        # to it, which share_of_net_aggregate_bias's own sign makes explicit.
        # A negative bias_sum whose own CI crosses zero is qualified as only
        # a POINT-ESTIMATE offset, not asserted as an established offset --
        # the CI itself has not ruled out that position's true bias_sum
        # being zero or even positive.
        by_position = primary[target]["by_position"]
        ranked = sorted(
            by_position.items(), key=lambda item: abs(item[1]["bias_sum"]), reverse=True
        )
        parts = []
        for position, stats in ranked:
            share = stats["share_of_net_aggregate_bias"]
            share_text = f"{share:+.1%} of net aggregate bias" if share is not None else "share undefined"
            offsets = ""
            if share is not None and share < 0.0:
                ci_crosses_zero = not _ci_excludes_zero(stats["mean_bias_bootstrap"])
                offsets = (
                    " (point estimate offsets the aggregate; CI crosses zero)"
                    if ci_crosses_zero
                    else " (offsets the aggregate)"
                )
            parts.append(
                f"{position}: bias_sum={stats['bias_sum']:+.2f} ({share_text}){offsets}, "
                f"mean_bias={stats['mean_bias']:+.4f}, rows={stats['rows']}, "
                f"CI [{stats['mean_bias_bootstrap']['ci_low']:.4f}, "
                f"{stats['mean_bias_bootstrap']['ci_high']:.4f}]"
            )
        return ", ".join(parts)

    def _phase_stability(target: str) -> str:
        by_phase = primary[target]["by_gameweek_phase"]
        parts = []
        for phase in ("early", "mid", "late"):
            stats = by_phase.get(phase)
            if stats is None:
                parts.append(f"{phase}: no rows")
                continue
            ci_low = stats["mean_bias_bootstrap"]["ci_low"]
            ci_high = stats["mean_bias_bootstrap"]["ci_high"]
            parts.append(
                f"{phase}: mean_bias={stats['mean_bias']:+.4f}, rows={stats['rows']} "
                f"distinct_gameweeks={stats['distinct_gameweeks']}, "
                f"CI [{ci_low:.4f}, {ci_high:.4f}] "
                f"({_stability_label(stats['distinct_gameweeks'], stats['mean_bias_bootstrap'])})"
            )
        return "; ".join(parts)

    # correction_recommendation is driven ONLY by contrast verdicts (never a
    # focus side's own CI) -- "High-end-only shrinkage is the next policy to
    # test" is permitted only when ALL FOUR required contrasts (both
    # top-band-vs-overall, both xPts-high-vs-same-window) show
    # verdict == "above_zero". Any other combination reports mixed/
    # inconclusive evidence target by target instead of a blanket claim.
    all_contrasts = {
        "start_probability_top_band": top_band_contrasts["start_probability"],
        "expected_minutes_top_band": top_band_contrasts["expected_minutes"],
        "xpts_high_band_start_probability": xpts_high_band_contrasts["start_probability"],
        "xpts_high_band_expected_minutes": xpts_high_band_contrasts["expected_minutes"],
    }
    all_concentrated = all(c["concentrated"] for c in all_contrasts.values())

    if all_concentrated:
        correction_recommendation = (
            "All four required paired contrasts (start_probability and expected_minutes top "
            "bands vs the primary cohort's overall bias; xpts_high_band_aligned vs the "
            "same-window xpts_same_window_aligned comparator) have a gameweek-cluster "
            "contrast CI lying entirely above zero -- bias is reliably concentrated in the "
            "high end for both targets, in both the primary cohort and the xPts high band "
            "specifically. High-end-only shrinkage is the next policy to test head-to-head "
            "against raw and global calibration; this concentration diagnostic does not yet "
            "establish that it improves Brier/MSE, MAE, or total xPts out of sample -- that "
            "requires a separate held-out policy comparison this script does not perform. "
            "Production shrinkage remains unchanged and unapplied."
        )
    else:
        mixed_parts = [
            f"{label}: {contrast['verdict']}"
            for label, contrast in all_contrasts.items()
        ]
        correction_recommendation = (
            "Mixed/inconclusive evidence across the four required contrasts -- "
            + "; ".join(mixed_parts)
            + ". 'High-end-only shrinkage is the next policy to test' requires ALL FOUR "
            "paired contrast CIs to lie entirely above zero; that is not the case here, so "
            "this diagnostic does NOT support a blanket high-end-only recommendation this "
            "run. See each contrast's own narrative under answers.top_band_vs_overall_contrast "
            "/ answers.xpts_high_band_vs_same_window_contrast for which target(s)/cohort(s) "
            "are and are not reliably concentrated. Production shrinkage remains unchanged "
            "and unapplied."
        )

    answers = {
        "top_band_vs_overall_contrast": top_band_contrasts,
        "xpts_high_band_vs_same_window_contrast": xpts_high_band_contrasts,
        "position_bias_contribution": {target: _position_ranking(target) for target in TARGETS},
        "gameweek_phase_bias": {target: _phase_stability(target) for target in TARGETS},
        "correction_recommendation": correction_recommendation,
        "contrast_methodology_note": (
            "'Concentrated'/'larger' claims are licensed ONLY by a paired gameweek-cluster "
            "contrast CI (focus_bias - comparator_bias, one shared cluster-label draw per "
            "replicate applied to both sides) lying entirely above zero -- never merely "
            "because a focus side's own individual CI excludes zero. Both sides can be "
            "individually significant and same-signed while their difference's own sampling "
            "uncertainty still crosses zero; see validation.appearance_segments.paired_"
            "contrast_bootstrap's own docstring."
        ),
        "terminology_note": (
            "This segment statistic is mean(predicted - actual): average over/under-prediction "
            "bias, not model 'confidence' or prediction extremeness. The pooled walk-forward "
            "slope in assess_appearance_calibration.py's own output IS evidence of "
            "overconfidence (slope < 1.0 means predictions are systematically too extreme); "
            "this script's segmented mean bias is evidence of WHERE average overprediction is "
            "concentrated -- a related but distinct claim, not interchangeable wording for the "
            "same thing."
        ),
        "causal_caveat": (
            "These are associations within the appearance model's own predictions vs "
            "realised outcomes, not a causal decomposition of total xPts error: the per-90 "
            "rate components built on top of start_probability/expected_minutes are highly "
            "correlated with them in exactly the bands this script finds most biased, so a "
            "large appearance-model bias in a segment is consistent with -- but does not "
            "prove -- that segment's total xPts error being driven primarily by the "
            "appearance model rather than those correlated rate components."
        ),
    }

    return {
        "$schema_note": (
            "Read-only segment diagnostic over the appearance model's own already-scored "
            "walk-forward predictions (start_probability/expected_minutes vs realised "
            "actual_started/actual_minutes), across four cohorts (appearance_eligible: "
            "primary; xpts_scored_aligned, xpts_high_band_aligned, xpts_same_window_aligned: "
            "sensitivity) and four fixed-boundary segment axes. Any 'concentrated'/'larger' "
            "claim is licensed ONLY by a paired gameweek-cluster CONTRAST bootstrap (see "
            "answers.top_band_vs_overall_contrast / "
            "answers.xpts_high_band_vs_same_window_contrast and "
            "answers.contrast_methodology_note) -- never by a single side's own CI excluding "
            "zero. Pairs with "
            "docs/research/walk_forward_benchwarmers_2025_26_appearance_calibration.json "
            "(this script's segment breakdown of that script's pooled finding) and "
            "docs/research/walk_forward_benchwarmers_2025_26_calibration.json (source of "
            "the xpts_high_band_aligned/xpts_same_window_aligned membership rule). "
            "Introduces no scoring/formula change; not applied to production."
        ),
        "season": season,
        "import_run_id": imported.import_run_id,
        "evaluation_from_gw": evaluation_from_gw,
        "evaluation_to_gw": evaluation_to_gw,
        "gameweek_phase_boundaries": {
            "early": f"GW1-{GAMEWEEK_PHASE_MID_START_GW - 1}",
            "mid": f"GW{GAMEWEEK_PHASE_MID_START_GW}-{GAMEWEEK_PHASE_LATE_START_GW - 1}",
            "late": f"GW{GAMEWEEK_PHASE_LATE_START_GW}-38",
            "convention": (
                "Fixed canonical season thirds -- a new diagnostic convention documented in "
                "validation.appearance_segments' own module docstring, NOT "
                "docs/DATA_MODEL.md's preseason short-form rate windows (a previous-season "
                "rate-history boundary for an unrelated purpose, not a meaningful "
                "current-season phase boundary for this diagnostic; that scheme also produced "
                "a lopsided 26/4/6-gameweek split over the default GW3-38 evaluation range)."
            ),
            "minimum_gameweek_clusters_for_stable_label": MINIMUM_GAMEWEEK_CLUSTERS_FOR_STABLE_LABEL,
        },
        "high_band_percentile": high_band_percentile,
        "xpts_minimum_calibration_gameweeks": xpts_minimum_calibration_gameweeks,
        "self_check": self_check,
        "coverage": coverage,
        "appearance_eligible": cohort_results["appearance_eligible"],
        "xpts_scored_aligned": cohort_results["xpts_scored_aligned"],
        "xpts_high_band_aligned": cohort_results["xpts_high_band_aligned"],
        "xpts_same_window_aligned": cohort_results["xpts_same_window_aligned"],
        "answers": answers,
        "methodology_notes": [
            "appearance_eligible (primary cohort), xpts_scored_aligned (sensitivity) are "
            "exactly assess_appearance_calibration.py's own two cohorts, built the same way "
            "(xpts_scored_aligned_observations against xpts_keys_from_backtest_observations).",
            "xpts_high_band_aligned and xpts_same_window_aligned (new sensitivity cohorts) "
            "are BOTH derived from ONE walk_forward_calibration call over "
            "backtest.observations, so they share the same eligible-gameweek window by "
            "construction: xpts_high_band_aligned restricts xpts_scored_aligned to keys in "
            "that call's high_band_evaluation_rows (the out-of-sample, prior-only "
            "75th-percentile-of-predicted_xpts band); xpts_same_window_aligned restricts it "
            "to keys in that SAME call's overall_evaluation_rows (every eligible gameweek's "
            "own evaluation rows, read directly rather than inferred from "
            "xpts_high_band_aligned's own min/max gameweek, since an eligible gameweek can "
            "legitimately contribute zero high-band rows). xpts_same_window_aligned -- NOT "
            "the wider xpts_scored_aligned, which spans every evaluated gameweek including "
            "cold-start ones before the xPts calibration's own warm-up completes -- is the "
            "correct comparator for any xpts_high_band_aligned claim.",
            "No per-segment regression slope is ever fit -- see "
            "validation.appearance_segments module docstring. Each segment reports mean "
            "bias (predicted - actual) and bias_sum (mean_bias * rows, additive across a "
            "partition), a gameweek-cluster bootstrap CI for mean_bias, and Brier "
            "score/observed start rate (start_probability) or MSE/MAE (expected_minutes). "
            "insufficient_variation is True when a segment's own predicted values have "
            "fewer than 2 rows or zero variance.",
            "Segment bands are FIXED absolute thresholds (start_probability: [0,.2), "
            "[.2,.4), [.4,.6), [.6,.8), [.8,1]; expected_minutes: [0,15), [15,30), [30,45), "
            "[45,60), [60,75), [75,90]), not population-adaptive quantiles -- a band's "
            "meaning is identical across cohorts and reruns.",
            "gameweek_phase boundaries are fixed canonical season thirds (GW1-13/14-26/27-38) "
            "-- see gameweek_phase_boundaries.convention -- not derived from the evaluated "
            "range or any population statistic. A phase's stability wording is withheld "
            "(see stability_label / MINIMUM_GAMEWEEK_CLUSTERS_FOR_STABLE_LABEL) whenever its "
            "own distinct_gameweeks is below the minimum, regardless of what its CI shows, "
            "since a percentile bootstrap from very few clusters can exclude zero by chance.",
            "Every segment axis is a strict partition of its parent cohort's rows -- each "
            "axis's segment row counts sum back exactly to the cohort's total row count "
            "(validate_segment_partition, checked at build time, not assumed).",
            "Gameweek-cluster bootstrap CIs use the same cluster-resampling scheme as "
            "validation.paired_uncertainty.block_bootstrap_statistic, but computed from "
            "precomputed per-gameweek sufficient statistics (count, sum of predicted-actual) "
            "for runtime only -- mean bias is linear in rows, so this reproduces that "
            "primitive's point estimate/CI to floating-point tolerance, never a materially "
            "different result (see validation.appearance_segments module docstring and its "
            "dedicated equivalence test).",
            "A single side's own CI excluding zero is NOT sufficient to call a comparison "
            "'concentrated'/'larger': both the focus and comparator can be individually "
            "significant and same-signed while their difference's own sampling uncertainty "
            "still crosses zero. Every 'concentrated'/'larger' claim in this JSON is licensed "
            "ONLY by validation.appearance_segments.paired_contrast_bootstrap's own paired "
            "gameweek-cluster contrast CI (one shared cluster-label draw applied to both "
            "sides per replicate, never two independently-bootstrapped CIs subtracted) -- see "
            "answers.top_band_vs_overall_contrast, "
            "answers.xpts_high_band_vs_same_window_contrast, and "
            "answers.contrast_methodology_note.",
            "Terminology: this diagnostic's segment statistic is average over/under-prediction "
            "bias (mean(predicted - actual)), not model confidence -- see answers.terminology_note.",
            "Association, not causation: see answers.causal_caveat and the LIMITATIONS list.",
        ],
        "limitations": LIMITATIONS,
        "reproduce": (
            ".venv\\Scripts\\python.exe scripts/diagnose_appearance_segments.py --season 2025-26"
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
        xpts_minimum_calibration_gameweeks=args.xpts_minimum_calibration_gameweeks,
        high_band_percentile=args.high_band_percentile,
        resamples=args.resamples,
        seed=args.seed,
        reference_path=args.reference,
    )
    output = json.dumps(result, indent=2, sort_keys=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output + "\n", encoding="utf-8")

    print(f"self_check against {args.reference}: {result['self_check']['status']}")
    print(f"coverage: {result['coverage']}")
    print()
    for cohort in COHORTS:
        for target in TARGETS:
            overall = result[cohort][target]["overall"]
            print(
                f"{cohort}/{target}: rows={overall['rows']} "
                f"mean_bias={overall['mean_bias']:+.4f} "
                f"ci=[{overall['mean_bias_bootstrap']['ci_low']:.4f}, "
                f"{overall['mean_bias_bootstrap']['ci_high']:.4f}]"
            )
    print()
    print("Paired contrast bootstraps (the sole basis for any 'concentrated' claim):")
    for label, contrast in result["answers"]["top_band_vs_overall_contrast"].items():
        _print_contrast(f"top_band_vs_overall/{label}", contrast)
    for label, contrast in result["answers"]["xpts_high_band_vs_same_window_contrast"].items():
        _print_contrast(f"xpts_high_band_vs_same_window/{label}", contrast)
    print()
    print("Answers:")
    for key, value in result["answers"].items():
        print(f"  {key}: {value}")
    print(f"\nWrote {args.output}")


def _print_contrast(label: str, contrast: dict[str, object]) -> None:
    if contrast["contrast_bootstrap"] is None:
        print(f"  {label}: {contrast['narrative']}")
        return
    ci = contrast["contrast_bootstrap"]
    print(
        f"  {label}: focus={contrast['focus_bias']:+.6f} comparator={contrast['comparator_bias']:+.6f} "
        f"contrast={contrast['contrast_point_estimate']:+.6f} "
        f"ci=[{ci['ci_low']:.6f}, {ci['ci_high']:.6f}] "
        f"shared_gw={contrast['shared_distinct_gameweeks']} verdict={contrast['verdict']}"
    )


if __name__ == "__main__":
    main()
