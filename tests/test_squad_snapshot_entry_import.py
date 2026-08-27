"""Coverage for importing a squad snapshot from FPL's public entry-picks API.

Reuses test_squad_snapshot.py's own `_database` fixture (15 players, fpl_id
1-15, priced at (50 + index) / 10) so the two import paths (CSV and live
fetch) are tested against the identical official-identity backdrop.
"""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pytest

from fpl_model.ingest.squad_snapshot import (
    import_squad_snapshot_from_entry,
    validate_entry_picks_payload,
)
from tests.test_squad_snapshot import _database

SQUAD_CAPTURED_AT = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)


def _picks_payload(*, bank: int = 5, captain_fpl_id: int = 9, vice_fpl_id: int = 5) -> dict:
    return {
        "active_chip": None,
        "entry_history": {"event": 1, "bank": bank, "value": 1005},
        "picks": [
            {
                "element": fpl_id,
                "position": fpl_id,
                "multiplier": 2 if fpl_id == captain_fpl_id else 1,
                "is_captain": fpl_id == captain_fpl_id,
                "is_vice_captain": fpl_id == vice_fpl_id,
                "element_type": 1,
            }
            for fpl_id in range(1, 16)
        ],
    }


def _import(tmp_path, **overrides):
    database_path = overrides.pop("database_path", None) or _database(tmp_path)
    payload = overrides.pop("picks_payload", None) or _picks_payload()
    arguments = {
        "entry_id": 123456,
        "entry_name": "Test Entry",
        "season": "2026-27",
        "target_gameweek": 1,
        "source_ingestion_run_id": "official",
        "captured_at": SQUAD_CAPTURED_AT,
        "free_transfers": None,
        "unlimited_transfers": True,
        "chip_period": 1,
        "chip_states": {
            "wildcard": "available",
            "free_hit": "available",
            "bench_boost": "available",
            "triple_captain": "available",
        },
        "database_path": database_path,
    }
    arguments.update(overrides)
    return import_squad_snapshot_from_entry(payload, **arguments)


def test_imports_a_live_squad_and_flags_estimated_selling_price(tmp_path):
    result = _import(tmp_path)

    assert result.player_rows == 15
    assert result.selling_price_is_estimated is True
    # team_value_tenths is bank_tenths + sum(selling_price_tenths): players
    # sum to (50+1)/10 .. (50+15)/10 in tenths == 51..65, plus the default
    # fixture's bank of 5 (FPL tenths, unconverted -- see
    # test_bank_is_read_from_entry_history for the unit-conversion check).
    assert result.team_value_tenths == sum(range(51, 66)) + 5


def test_purchase_and_selling_price_are_estimated_from_current_market_price(tmp_path):
    database_path = _database(tmp_path)
    result = _import(tmp_path, database_path=database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        rows = connection.execute(
            "SELECT fpl_id, purchase_price_tenths, selling_price_tenths "
            "FROM squad_snapshot_player WHERE squad_snapshot_id = ? ORDER BY fpl_id",
            [result.squad_snapshot_id],
        ).fetchall()

    for fpl_id, purchase_tenths, selling_tenths in rows:
        expected = 50 + fpl_id
        assert purchase_tenths == expected
        assert selling_tenths == expected


def test_bank_is_read_from_entry_history(tmp_path):
    database_path = _database(tmp_path)
    result = _import(
        tmp_path, database_path=database_path, picks_payload=_picks_payload(bank=25)
    )

    with duckdb.connect(str(database_path), read_only=True) as connection:
        bank_tenths = connection.execute(
            "SELECT bank_tenths FROM squad_snapshot WHERE squad_snapshot_id = ?",
            [result.squad_snapshot_id],
        ).fetchone()[0]
    assert bank_tenths == 25


def test_source_label_names_the_estimation_caveat(tmp_path):
    database_path = _database(tmp_path)
    result = _import(tmp_path, database_path=database_path)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        source_label = connection.execute(
            "SELECT source_label FROM squad_snapshot WHERE squad_snapshot_id = ?",
            [result.squad_snapshot_id],
        ).fetchone()[0]
    assert "estimated" in source_label.lower()


def test_rejects_a_payload_with_more_than_one_captain(tmp_path):
    payload = _picks_payload()
    payload["picks"][0]["is_captain"] = True  # a second captain, alongside fpl_id=9

    with pytest.raises(ValueError, match="exactly one captain"):
        _import(tmp_path, picks_payload=payload)


def test_rejects_a_payload_with_no_vice_captain(tmp_path):
    payload = _picks_payload()
    payload["picks"][4]["is_vice_captain"] = False  # was fpl_id=5

    with pytest.raises(ValueError, match="exactly one vice-captain"):
        _import(tmp_path, picks_payload=payload)


def test_rejects_a_payload_missing_the_picks_list(tmp_path):
    payload = _picks_payload()
    del payload["picks"]

    with pytest.raises(ValueError, match="picks"):
        _import(tmp_path, picks_payload=payload)


def test_rejects_a_payload_with_duplicate_elements(tmp_path):
    payload = _picks_payload()
    payload["picks"][1]["element"] = payload["picks"][0]["element"]

    with pytest.raises(ValueError, match="duplicate element"):
        _import(tmp_path, picks_payload=payload)


def test_rejects_a_player_absent_from_the_official_snapshot(tmp_path):
    payload = _picks_payload()
    payload["picks"][0]["element"] = 9999

    with pytest.raises(ValueError, match="absent from source FPL snapshot"):
        _import(tmp_path, picks_payload=payload)


def test_validate_entry_picks_payload_returns_the_private_rows_contract(tmp_path):
    payload = _picks_payload()
    rows = validate_entry_picks_payload(payload)

    assert sorted(rows["fpl_id"].tolist()) == list(range(1, 16))
    assert set(rows.columns) == {"fpl_id", "squad_position", "is_captain", "is_vice_captain"}
    assert rows.loc[rows["fpl_id"] == 9, "is_captain"].iloc[0]
    assert rows.loc[rows["fpl_id"] == 5, "is_vice_captain"].iloc[0]


def test_two_imports_of_the_same_payload_are_idempotent(tmp_path):
    database_path = _database(tmp_path)
    first = _import(tmp_path, database_path=database_path)
    second = _import(tmp_path, database_path=database_path)

    assert first.squad_snapshot_id == second.squad_snapshot_id
    with duckdb.connect(str(database_path), read_only=True) as connection:
        count = connection.execute(
            "SELECT count(*) FROM squad_snapshot WHERE squad_snapshot_id = ?",
            [first.squad_snapshot_id],
        ).fetchone()[0]
    assert count == 1
