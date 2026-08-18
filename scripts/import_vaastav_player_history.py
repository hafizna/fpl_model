"""Archive and import a pinned Vaastav player-fixture history revision."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fpl_model.ingest.player_history import (
    SOURCE_NAME,
    import_player_fixture_history,
    materialize_preseason_rate_history,
)
from fpl_model.ingest.vaastav import VaastavClient
from fpl_model.storage import DEFAULT_DATABASE_PATH


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", required=True)
    parser.add_argument(
        "--revision",
        help="Exact Git revision; defaults to the newest revision of merged_gw.csv",
    )
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw/vaastav"))
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    client = VaastavClient()
    revisions = client.file_revisions(
        args.season,
        filename="gws/merged_gw.csv",
    )
    if not revisions:
        raise ValueError(f"no merged_gw.csv revisions found for {args.season}")
    if args.revision:
        matches = [item for item in revisions if item.sha == args.revision]
        if not matches:
            raise ValueError("requested revision did not modify merged_gw.csv")
        revision = matches[-1]
    else:
        revision = revisions[-1]

    players = client.csv_at_revision(args.season, "players_raw.csv", revision)
    gameweeks = client.csv_at_revision(
        args.season,
        "gws/merged_gw.csv",
        revision,
    )
    archive_dir = args.raw_root / args.season / revision.sha
    archive_dir.mkdir(parents=True, exist_ok=True)
    players_path = archive_dir / "players_raw.csv"
    gameweeks_path = archive_dir / "merged_gw.csv"
    players.to_csv(players_path, index=False, lineterminator="\n")
    gameweeks.to_csv(gameweeks_path, index=False, lineterminator="\n")

    imported = import_player_fixture_history(
        players_path,
        gameweeks_path,
        season=args.season,
        source_revision=revision.sha,
        source_committed_at=revision.committed_at,
        database_path=args.database,
    )
    rates = materialize_preseason_rate_history(
        source_import_run_id=imported.import_run_id,
        database_path=args.database,
    )
    manifest = {
        "source": SOURCE_NAME,
        "season": args.season,
        "source_revision": revision.sha,
        "source_committed_at": revision.committed_at.isoformat(),
        "players_sha256": imported.players_sha256,
        "gameweeks_sha256": imported.gameweeks_sha256,
        "player_rows": imported.player_rows,
        "fixture_rows": imported.fixture_rows,
        "exact_duplicate_rows_removed": imported.exact_duplicate_rows_removed,
        "import_run_id": imported.import_run_id,
        "rate_run_id": rates.rate_run_id,
    }
    (archive_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"Stored {imported.import_run_id}: season={args.season} "
        f"players={imported.player_rows} fixtures={imported.fixture_rows} "
        f"deduplicated={imported.exact_duplicate_rows_removed}"
    )
    print(f"Materialised {rates.rate_run_id}: players={rates.player_rows}")
    print(f"Archived pinned source under {archive_dir.resolve()}")


if __name__ == "__main__":
    main()
