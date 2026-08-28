"""Coverage for coverage/freshness flowing from a release file into
webapp.service's bootstrap/lineup/transfer responses.

Reuses test_webapp_service.py's own `_release_file` fixture, extended with a
`coverage`/`freshness` block matching what `release_export.build_web_release`
now writes, so this exercises the exact read-side shape a real release
carries -- not a hand-invented one.
"""

from __future__ import annotations

import json
from pathlib import Path

from fpl_model.webapp.service import (
    load_web_bootstrap,
    recommend_web_lineups,
    recommend_web_transfers,
)
from tests.test_webapp_service import _release_file


def _add_release_metadata(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["release"]["coverage"] = {
        "total_registered_players": 20,
        "fully_covered_players": 15,
        "excluded_missing_projection": 4,
        "excluded_partial_horizon_coverage": 1,
    }
    payload["release"]["freshness"] = {
        "passes": True,
        "problems": [],
        "gameweeks": [{"target_gameweek": 2, "fpl_finality": {"is_final": False}}],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_bootstrap_surfaces_coverage_and_freshness_from_the_release(tmp_path: Path):
    release_path = tmp_path / "release.json"
    _release_file(release_path)
    _add_release_metadata(release_path)

    bootstrap = load_web_bootstrap(tmp_path / "missing.duckdb", release_path=release_path)

    assert bootstrap["release"]["coverage"]["fully_covered_players"] == 15
    assert bootstrap["release"]["freshness"]["passes"] is True


def test_database_mode_reports_no_release_metadata(tmp_path: Path):
    from tests.test_webapp_service import _database

    database_path = tmp_path / "web.duckdb"
    _database(database_path)

    bootstrap = load_web_bootstrap(database_path)

    assert bootstrap["release"]["coverage"] is None
    assert bootstrap["release"]["freshness"] is None


def test_recommend_lineups_and_transfers_also_carry_release_metadata(tmp_path: Path):
    release_path = tmp_path / "release.json"
    fpl_ids = _release_file(release_path)
    _add_release_metadata(release_path)

    lineups = recommend_web_lineups(fpl_ids, release_path=release_path)
    transfers = recommend_web_transfers(fpl_ids, release_path=release_path)

    assert lineups["coverage"]["excluded_partial_horizon_coverage"] == 1
    assert lineups["freshness"]["passes"] is True
    assert transfers["coverage"]["excluded_partial_horizon_coverage"] == 1
    assert transfers["freshness"]["passes"] is True
