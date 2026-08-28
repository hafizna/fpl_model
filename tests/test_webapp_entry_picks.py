"""Coverage for webapp.service.resolve_entry_picks.

Reuses test_webapp_service.py's own `_release_file` fixture (15 players,
fpl_id 1-15, priced at 5.0m each) so this exercises the exact DB-free,
compact-release deployment mode the web app runs under in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fpl_model.webapp.service import resolve_entry_picks
from tests.test_webapp_service import _release_file


def _picks_payload(*, bank: int = 5, captain_fpl_id: int = 9, vice_fpl_id: int = 5) -> dict:
    pick_order = (1, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14, 2, 6, 7, 15)
    return {
        "active_chip": None,
        "entry_history": {"event": 2, "bank": bank, "value": 1005},
        "picks": [
            {
                "element": fpl_id,
                "position": position,
                "multiplier": 2 if fpl_id == captain_fpl_id else 1,
                "is_captain": fpl_id == captain_fpl_id,
                "is_vice_captain": fpl_id == vice_fpl_id,
                "element_type": 1,
            }
            for position, fpl_id in enumerate(pick_order, start=1)
        ],
    }


def test_resolves_picks_against_the_release_catalog(tmp_path: Path):
    release_path = tmp_path / "release.json"
    _release_file(release_path)

    result = resolve_entry_picks(_picks_payload(), release_path=release_path)

    assert sorted(result["fpl_ids"]) == list(range(1, 16))
    assert result["bank_tenths"] == 5
    assert result["captain_fpl_id"] == 9
    assert result["vice_captain_fpl_id"] == 5
    assert result["starter_fpl_ids"] == [1, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14]
    assert result["bench_fpl_ids"] == [2, 6, 7, 15]
    assert result["selling_price_is_estimated"] is True
    # _release_file prices every player at 50 tenths.
    assert all(price == 50 for price in result["selling_prices"].values())
    assert set(result["selling_prices"]) == set(result["fpl_ids"])


def test_rejects_a_player_absent_from_the_horizon(tmp_path: Path):
    release_path = tmp_path / "release.json"
    _release_file(release_path)
    payload = _picks_payload()
    payload["picks"][0]["element"] = 9999

    with pytest.raises(ValueError, match="lack complete horizon projections"):
        resolve_entry_picks(payload, release_path=release_path)


def test_rejects_a_payload_missing_bank(tmp_path: Path):
    release_path = tmp_path / "release.json"
    _release_file(release_path)
    payload = _picks_payload()
    del payload["entry_history"]["bank"]

    with pytest.raises(ValueError, match="entry_history.bank"):
        resolve_entry_picks(payload, release_path=release_path)


def test_no_server_side_write_occurs(tmp_path: Path):
    """resolve_entry_picks touches only the already-loaded catalog -- no new
    database file or table should be created by calling it."""
    release_path = tmp_path / "release.json"
    _release_file(release_path)
    database_path = tmp_path / "should_not_be_created.duckdb"

    resolve_entry_picks(_picks_payload(), database_path=database_path, release_path=release_path)

    assert not database_path.exists()
