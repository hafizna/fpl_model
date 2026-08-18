"""Segment the walk-forward backtest's own scored rows to locate model bias.

This is a read-only diagnostic over already-scored observations from
``materialize_benchwarmers_walk_forward_backtest`` (the same function
``scripts/backtest_benchwarmers.py`` calls) -- it never calls any
``project_benchwarmers_*``/``weight_*`` component function differently and
never introduces a second scoring path. Its purpose is to locate *where* the
aggregate model bias (mean_error, currently +0.118 across all scored rows)
concentrates, before any shrinkage/calibration work changes a formula.

Segments:

- position (GK/DEF/MID/FWD)
- gameweek, and an early/late season split at the evaluated range's midpoint
- expected-minutes quartile bands (population-adaptive, not fixed thresholds)
- start-probability quartile bands
- predicted-xPts quartile bands
- coverage/gap rate by position and by gap flag
- a triangulated "bias decomposition": each segment's mean predicted
  component contributions (pre-home-away) alongside its mean_error, plus its
  share of the total predicted-minus-actual bias mass. Vaastav's total_points
  has no component breakdown, so this cannot be a literal actual-vs-predicted
  per-component split -- it shows which components dominate a biased
  segment's prediction, not which component "caused" the bias.

Quartile band boundaries are the one place this script makes an explicit,
overridable methodological choice rather than a fixed cutoff; see
``_quartile_bands``.

Before writing any segment output, this run's recomputed aggregate metrics
are compared against ``--reference`` (defaulting to the committed
``docs/research/walk_forward_benchwarmers_2025_26.json``) via
``validation.backtest_self_check.verify_self_check``. A mismatch raises
``ValueError`` and nothing is written -- see that module for the exact
fields compared and the floating-point tolerance used.
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
from fpl_model.validation.backtest import score_predictions
from fpl_model.validation.backtest_self_check import load_reference, verify_self_check
from fpl_model.validation.benchwarmers_backtest import (
    materialize_benchwarmers_walk_forward_backtest,
)
from fpl_model.validation.matched_naive import matched_naive_observations

COMPONENT_COLUMNS = (
    "component_appearance",
    "component_sixty_minutes",
    "component_saves",
    "component_yellow_cards",
    "component_red_cards",
    "component_bonus",
    "component_assists",
    "component_goals",
    "component_clean_sheet",
    "component_goals_conceded",
    "component_defcon",
)


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
        "--output",
        type=Path,
        default=Path("docs/research/walk_forward_benchwarmers_2025_26_segments.json"),
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

    Duplicates ``scripts/backtest_benchwarmers.py``'s own small bootstrap
    rather than importing a private helper across script files -- ``scripts/``
    has no ``__init__.py`` and is not an installed package, so each CLI script
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


def _quartile_bands(values: pd.Series, *, label: str) -> pd.Series:
    """Return population-adaptive quartile band labels for ``values``.

    Quartile cut points (not fixed thresholds) are the recommended default:
    they are computed from the scored population itself, so a band is never
    empty regardless of the underlying metric's scale. ``qcut`` collapses
    duplicate edges (common for a metric with many identical values, e.g.
    start_probability clustering at 0/1) so fewer than 4 distinct bands can
    result -- this is reported as-is, not padded with empty bands.
    """
    try:
        bands = pd.qcut(values, 4, duplicates="drop")
    except ValueError:
        # Fewer than 2 distinct values -- every row falls in one band.
        low, high = values.min(), values.max()
        return pd.Series([f"{label} {low:.1f}-{high:.1f}"] * len(values), index=values.index)
    return bands.apply(lambda interval: f"{label} {interval.left:.1f}-{interval.right:.1f}")


def _segment_table(frame: pd.DataFrame, group_column: str) -> dict[str, dict[str, object]]:
    """Return per-group model/matched-naive metrics plus bias-mass share."""
    total_bias_mass = (frame["predicted_xpts"] - frame["actual_points"]).sum()
    result: dict[str, dict[str, object]] = {}
    for key, group in frame.groupby(group_column, observed=True):
        group_bias_mass = (group["predicted_xpts"] - group["actual_points"]).sum()
        model_observations = tuple(group["_model_observation"])
        naive_observations = tuple(group["_naive_observation"])
        model_metrics = score_predictions(model_observations)
        naive_metrics = score_predictions(naive_observations)
        result[str(key)] = {
            "rows": len(group),
            "model_mae": model_metrics.mean_absolute_error,
            "model_rmse": model_metrics.root_mean_squared_error,
            "model_mean_error": model_metrics.mean_error,
            "matched_naive_mae": naive_metrics.mean_absolute_error,
            "matched_naive_rmse": naive_metrics.root_mean_squared_error,
            "mean_predicted_xpts": float(group["predicted_xpts"].mean()),
            "mean_actual_points": float(group["actual_points"].mean()),
            "bias_mass_share": (
                float(group_bias_mass / total_bias_mass) if total_bias_mass else None
            ),
            "mean_component_contribution": {
                column.removeprefix("component_"): float(group[column].mean())
                for column in COMPONENT_COLUMNS
            },
        }
    return result


def _gap_tables(gaps, diagnostics_by_position_total: dict[str, int]) -> tuple[dict, dict]:
    gap_by_position: dict[str, int] = {}
    gap_flag_by_position: dict[str, dict[str, int]] = {}
    for gap in gaps:
        gap_by_position[gap.position] = gap_by_position.get(gap.position, 0) + 1
        flags_for_position = gap_flag_by_position.setdefault(gap.position, {})
        for flag in gap.flags:
            flags_for_position[flag] = flags_for_position.get(flag, 0) + 1

    gap_rate_by_position: dict[str, float | None] = {}
    all_positions = set(gap_by_position) | set(diagnostics_by_position_total)
    for position in sorted(all_positions):
        scored = diagnostics_by_position_total.get(position, 0)
        gapped = gap_by_position.get(position, 0)
        candidates = scored + gapped
        gap_rate_by_position[position] = gapped / candidates if candidates else None
    return gap_rate_by_position, gap_flag_by_position


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

    model_metrics = score_predictions(backtest.observations)
    naive_observations = matched_naive_observations(backtest.observations, gameweeks_with_code)

    # Raises ValueError (before anything is written) if this run's recomputed
    # aggregate metrics diverge from the reference production backtest.
    self_check = verify_self_check(
        reference=reference,
        reference_path=reference_path,
        import_run_id=imported.import_run_id,
        evaluation_from_gw=evaluation_from_gw,
        evaluation_to_gw=evaluation_to_gw,
        model_metrics=model_metrics,
    )

    # Build one tidy frame: diagnostics + the matched observation pair, keyed
    # by (player_code, fixture_id, gameweek).
    diagnostics_frame = pd.DataFrame(
        [
            {
                "player_code": d.player_code,
                "fixture_id": d.fixture_id,
                "gameweek": d.gameweek,
                "position": d.position,
                "start_probability": d.start_probability,
                "expected_minutes": d.expected_minutes,
                "predicted_xpts": d.predicted_xpts,
                "actual_points": d.actual_points,
                **{column: getattr(d, column) for column in COMPONENT_COLUMNS},
            }
            for d in backtest.diagnostics
        ]
    )
    model_by_key = {
        (o.player_id, o.fixture_id, o.gameweek): o for o in backtest.observations
    }
    naive_by_key = {
        (o.player_id, o.fixture_id, o.gameweek): o for o in naive_observations
    }
    diagnostics_frame["_model_observation"] = diagnostics_frame.apply(
        lambda row: model_by_key[(row["player_code"], row["fixture_id"], row["gameweek"])],
        axis=1,
    )
    diagnostics_frame["_naive_observation"] = diagnostics_frame.apply(
        lambda row: naive_by_key[(row["player_code"], row["fixture_id"], row["gameweek"])],
        axis=1,
    )

    midpoint = (evaluation_from_gw + evaluation_to_gw) // 2
    diagnostics_frame["season_half"] = diagnostics_frame["gameweek"].apply(
        lambda gw: "early" if gw <= midpoint else "late"
    )
    diagnostics_frame["expected_minutes_band"] = _quartile_bands(
        diagnostics_frame["expected_minutes"], label="minutes"
    )
    diagnostics_frame["start_probability_band"] = _quartile_bands(
        diagnostics_frame["start_probability"], label="start_prob"
    )
    diagnostics_frame["predicted_xpts_band"] = _quartile_bands(
        diagnostics_frame["predicted_xpts"], label="xpts"
    )

    by_position = _segment_table(diagnostics_frame, "position")
    by_gameweek = _segment_table(diagnostics_frame, "gameweek")
    by_season_half = _segment_table(diagnostics_frame, "season_half")
    by_expected_minutes_band = _segment_table(diagnostics_frame, "expected_minutes_band")
    by_start_probability_band = _segment_table(diagnostics_frame, "start_probability_band")
    by_predicted_xpts_band = _segment_table(diagnostics_frame, "predicted_xpts_band")

    diagnostics_by_position_total = (
        diagnostics_frame.groupby("position", observed=True).size().to_dict()
    )
    gap_rate_by_position, gap_flag_by_position = _gap_tables(
        backtest.gaps, diagnostics_by_position_total
    )

    return {
        "$schema_note": (
            "Read-only segment diagnostic over an already-scored walk-forward "
            "backtest run; introduces no scoring/formula change. Pairs with "
            "docs/research/walk_forward_benchwarmers_2025_26.json."
        ),
        "season": season,
        "import_run_id": imported.import_run_id,
        "evaluation_from_gw": evaluation_from_gw,
        "evaluation_to_gw": evaluation_to_gw,
        "season_half_split_gw": midpoint,
        "self_check": self_check,
        "by_position": by_position,
        "by_gameweek": {
            str(k): v for k, v in sorted(by_gameweek.items(), key=lambda item: int(item[0]))
        },
        "by_season_half": by_season_half,
        "by_expected_minutes_band": by_expected_minutes_band,
        "by_start_probability_band": by_start_probability_band,
        "by_predicted_xpts_band": by_predicted_xpts_band,
        "gap_rate_by_position": gap_rate_by_position,
        "gap_flag_by_position": gap_flag_by_position,
        "methodology_notes": [
            "Quartile band boundaries are computed from the scored population "
            "itself (pandas qcut), not fixed thresholds, so bands adapt to "
            "the run's own distribution and are never empty by construction.",
            "mean_component_contribution values are pre-home-away "
            "(ScoringComponents.total_xpts), not predicted_xpts itself, which "
            "has the fixture's 1.05/0.95 home/away multiplier applied once.",
            "bias_mass_share is each segment's share of "
            "sum(predicted_xpts - actual_points) across all scored rows -- it "
            "shows which segment contributes most to the aggregate bias, "
            "weighted by row count, not which segment is most biased per-row "
            "(see the segment's own mean_error for that).",
            "This is a triangulation, not a causal component attribution: "
            "Vaastav's total_points has no component breakdown, so no exact "
            "actual-vs-predicted-per-component split is possible.",
        ],
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
        reference_path=args.reference,
    )
    output = json.dumps(result, indent=2, sort_keys=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output + "\n", encoding="utf-8")

    print(f"self_check against {args.reference}: {result['self_check']['status']}")
    print("\nBy position:")
    for position, stats in sorted(result["by_position"].items()):
        print(
            f"  {position:4s} rows={stats['rows']:5d} "
            f"model_mae={stats['model_mae']:.4f} "
            f"mean_error={stats['model_mean_error']:+.4f} "
            f"matched_naive_mae={stats['matched_naive_mae']:.4f}"
        )
    print("\nBy season half:")
    for half, stats in sorted(result["by_season_half"].items()):
        print(
            f"  {half:6s} rows={stats['rows']:5d} "
            f"model_mae={stats['model_mae']:.4f} "
            f"mean_error={stats['model_mean_error']:+.4f} "
            f"matched_naive_mae={stats['matched_naive_mae']:.4f}"
        )
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
