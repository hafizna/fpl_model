"""Run the genuine walk-forward backtest of the replicated Benchwarmers model.

Unlike ``scripts/backtest_smoke.py``, this scores the full 11-component xPts
model (not a naive expanding-points mean) using Vaastav-only, deadline-safe
team strength, player rate windows, and appearance projections. See
``docs/PIPELINE_ARCHITECTURE.md`` and ``docs/DATA_MODEL.md`` for the
methodology and its documented divergences from the workbook GW1 baseline.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import duckdb
import pandas as pd

from fpl_model.ingest.player_history import import_player_fixture_history
from fpl_model.ingest.vaastav import RAW_BASE, VaastavClient
from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.backtest import BacktestMetrics, score_predictions, walk_forward_folds
from fpl_model.validation.benchwarmers_backtest import (
    POLICY_VERSION,
    TEAM_STRENGTH_POLICY_VERSION,
    materialize_benchwarmers_walk_forward_backtest,
)
from fpl_model.validation.matched_naive import matched_naive_observations

DEFAULT_LIMITATIONS = [
    "Team strength uses a Vaastav-only expanding-window derivation "
    "(vaastav_expanding_team_strength_v1), NOT the reviewed workbook rates used by "
    "the GW1 preseason baseline; docs/PIPELINE_ARCHITECTURE.md notes aggregating "
    "Vaastav player-level xG does not reproduce workbook team rates, so these two "
    "team-strength sources must not be compared as if interchangeable.",
    "Appearance uses an empirical per-fixture minutes distribution "
    "(project_appearance) instead of the workbook benchwarmers_appearance_probability/"
    "unused_substitute logic, because Vaastav cannot distinguish an unused "
    "substitute from a player outside the matchday squad.",
    "Defensive contributions (DefCon) are present and non-null for all of "
    "2025-26 in the local Vaastav archive; this is a genuine component score, not "
    "a placeholder, but the scoring rule itself is new for 2025-26 so no "
    "cross-season history exists to validate it against.",
    "Early gameweeks are excluded from evaluation as an insufficient-history "
    "cold start (see evaluation_from_gw); this is a materially larger cold-start "
    "exclusion than a fully deployed model would need once multiple seasons of "
    "Vaastav history are archived.",
    "Double-gameweek and blank-gameweek player-fixtures are excluded from "
    "evaluation entirely, not aggregated or specially scored; see gap_breakdown "
    "for counts.",
    "League-average bonus-per-start/bps-per-start/bonus-per-bps are derived "
    "from the same in-season expanding Vaastav window, not the workbook reviewed "
    "previous-PL-season constants used by the GW1 preseason baseline -- these two "
    "numbers are expected to diverge, especially early season.",
    "Promoted teams and any team/player with zero matches before a gameweek "
    "cutoff are excluded from that gameweek as a counted gap, never assigned a "
    "fabricated prior.",
    "matched_naive_metrics is the only apples-to-apples naive comparison: it is "
    "scored on exactly the same rows as the model. The older "
    "docs/research/walk_forward_smoke_2025_26.json expanding-mean MAE (~1.06) "
    "must not be compared directly to this run's model MAE -- that smoke result "
    "is evaluated over the full player population (GW2+), including many "
    "zero-point unavailable/unused-substitute rows the model excludes as gaps, "
    "which mechanically lowers its MAE.",
    "A structural deadline-safety fix now also requires "
    "kickoff_time + outcome_delay <= target_deadline (not just gameweek < N) for "
    "every rate/appearance/team-strength/league-average input and for the "
    "matched naive comparator, so a postponed fixture carrying an earlier "
    "gameweek label cannot leak into a later gameweek's prediction.",
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
        "--output",
        type=Path,
        default=Path("docs/research/walk_forward_benchwarmers_2025_26.json"),
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

    Mirrors ``scripts/import_vaastav_player_history.py``'s archive/import
    pattern so both scripts share one reproducible, content-addressed source.
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
) -> dict[str, object]:
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

    metrics = score_predictions(backtest.observations) if backtest.observations else None
    folds = walk_forward_folds(backtest.observations)
    gap_breakdown: Counter[str] = Counter()
    for gap in backtest.gaps:
        gap_breakdown.update(gap.flags)

    matched_naive_metrics: BacktestMetrics | None = None
    absolute_mae_improvement: float | None = None
    absolute_rmse_improvement: float | None = None
    relative_mae_improvement: float | None = None
    relative_rmse_improvement: float | None = None
    if backtest.observations:
        matched_naive = matched_naive_observations(backtest.observations, gameweeks_with_code)
        matched_naive_metrics = score_predictions(matched_naive)
        absolute_mae_improvement = (
            matched_naive_metrics.mean_absolute_error - metrics.mean_absolute_error
        )
        absolute_rmse_improvement = (
            matched_naive_metrics.root_mean_squared_error
            - metrics.root_mean_squared_error
        )
        relative_mae_improvement = (
            absolute_mae_improvement / matched_naive_metrics.mean_absolute_error
            if matched_naive_metrics.mean_absolute_error
            else None
        )
        relative_rmse_improvement = (
            absolute_rmse_improvement / matched_naive_metrics.root_mean_squared_error
            if matched_naive_metrics.root_mean_squared_error
            else None
        )

    scored_coverage = (
        backtest.scored_player_fixture_rows / backtest.candidate_player_fixture_rows
        if backtest.candidate_player_fixture_rows
        else None
    )

    return {
        "$schema_note": (
            "Genuine walk-forward backtest of the replicated 11-component "
            "Benchwarmers xPts model, not a smoke test."
        ),
        "label": "benchwarmers_replica_walk_forward_backtest",
        "is_benchwarmers_baseline": True,
        "policy_version": POLICY_VERSION,
        "team_strength_policy_version": TEAM_STRENGTH_POLICY_VERSION,
        "season": season,
        "source": f"{RAW_BASE}/{season}/gws/merged_gw.csv",
        "import_run_id": imported.import_run_id,
        "evaluation_from_gw": evaluation_from_gw,
        "evaluation_to_gw": evaluation_to_gw,
        "evaluated_gameweeks": list(backtest.evaluated_gameweeks),
        "deadline_method": "earliest GW kickoff minus 90 minutes (inferred, not archived)",
        "outcome_available_method": "kickoff plus 3 hours",
        "team_strength_window": (
            f"long-form: expanding all GW < N; short-form: trailing "
            f"min({short_form_gameweeks}, N-1) GW; blend "
            f"{long_form_weight:.1f}/{1.0 - long_form_weight:.1f}"
        ),
        "player_rate_window": (
            f"long-form: expanding all GW < N; short-form: trailing "
            f"{short_form_gameweeks} GW; DefCon short-form: trailing "
            f"{defcon_short_form_gameweeks} GW"
        ),
        "appearance_method": (
            "empirical per-fixture MinutesScenario distribution "
            "(project_appearance), expanding all GW < N; no unused_substitute "
            "reconstruction"
        ),
        "candidate_player_fixture_rows": backtest.candidate_player_fixture_rows,
        "scored_player_fixture_rows": backtest.scored_player_fixture_rows,
        "scored_coverage": scored_coverage,
        "gap_rows": len(backtest.gaps),
        "gap_breakdown": dict(sorted(gap_breakdown.items())),
        "walk_forward_folds": len(folds),
        "metrics": asdict(metrics) if metrics is not None else None,
        "matched_naive_metrics": (
            asdict(matched_naive_metrics) if matched_naive_metrics is not None else None
        ),
        "matched_naive_method": (
            "expanding mean of that player's own realised total_points, evaluated on "
            "exactly the same (player_code, fixture_id, gameweek) rows the model scored "
            "-- not the full player pool -- using the same causal "
            "kickoff_time + outcome_delay <= deadline availability rule; cold start "
            "(no prior available points) predicts 0.0"
        ),
        "absolute_mae_improvement": absolute_mae_improvement,
        "absolute_rmse_improvement": absolute_rmse_improvement,
        "relative_mae_improvement": relative_mae_improvement,
        "relative_rmse_improvement": relative_rmse_improvement,
        "limitations": DEFAULT_LIMITATIONS,
        "reproduce": ".venv\\Scripts\\python.exe scripts/backtest_benchwarmers.py --season 2025-26",
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
    )
    output = json.dumps(result, indent=2, sort_keys=True)
    print(output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
