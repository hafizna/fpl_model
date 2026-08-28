"""Run and publish one platform-neutral, fail-closed deadline refresh."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from fpl_model.operations.deadline_refresh import DeadlineRefreshConfig, run_deadline_refresh
from fpl_model.storage import DEFAULT_DATABASE_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gameweek", type=int, required=True)
    parser.add_argument("--current-season", default="2026-27")
    parser.add_argument("--previous-season", default="2025-26")
    parser.add_argument("--team-strength-csv", type=Path, required=True)
    parser.add_argument("--team-strength-source-label", required=True)
    parser.add_argument("--vaastav-players-csv", type=Path, required=True)
    parser.add_argument("--vaastav-source-revision", required=True)
    parser.add_argument("--calibration-artifact-id", required=True)
    parser.add_argument("--uncertainty-artifact-id", required=True)
    parser.add_argument("--previous-effective-fixtures", type=float, default=5.0)
    parser.add_argument("--allow-analytically-complete", action="store_true")
    parser.add_argument("--require-production", action="store_true")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--web-release-output", type=Path, default=Path("web/release.json"))
    parser.add_argument(
        "--status-output",
        type=Path,
        default=Path("outputs/deadline_refresh_status.json"),
    )
    parser.add_argument(
        "--materialization-output",
        type=Path,
        default=Path("outputs/inseason_release_materialization.json"),
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=Path("outputs/deadline_refresh.lock"),
    )
    parser.add_argument("--backup-directory", type=Path, default=Path("outputs/backups"))
    parser.add_argument("--alert-webhook-env", default="FPL_REFRESH_WEBHOOK_URL")
    parser.add_argument("--alert-timeout-seconds", type=float, default=10.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_deadline_refresh(
        DeadlineRefreshConfig(
            target_gameweek=args.gameweek,
            current_season=args.current_season,
            previous_season=args.previous_season,
            team_strength_csv=args.team_strength_csv,
            team_strength_source_label=args.team_strength_source_label,
            vaastav_players_csv=args.vaastav_players_csv,
            vaastav_source_revision=args.vaastav_source_revision,
            calibration_artifact_id=args.calibration_artifact_id,
            uncertainty_artifact_id=args.uncertainty_artifact_id,
            database_path=args.database,
            web_release_output=args.web_release_output,
            status_output=args.status_output,
            materialization_report_output=args.materialization_output,
            lock_file=args.lock_file,
            backup_directory=args.backup_directory,
            previous_effective_fixtures=args.previous_effective_fixtures,
            allow_analytically_complete=args.allow_analytically_complete,
            require_production=args.require_production,
            alert_webhook_url=os.environ.get(args.alert_webhook_env),
            alert_timeout_seconds=args.alert_timeout_seconds,
        )
    )
    print(json.dumps(result.report, indent=2, sort_keys=True))
    raise SystemExit(result.exit_code)


if __name__ == "__main__":
    main()
