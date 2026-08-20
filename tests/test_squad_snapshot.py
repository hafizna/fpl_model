from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pandas as pd
import pytest

from fpl_model.ingest.squad_snapshot import import_squad_snapshot_csv
from fpl_model.storage import initialize_database

OFFICIAL_CAPTURED_AT = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
SQUAD_CAPTURED_AT = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
DEADLINE = datetime(2026, 8, 21, 17, 30, tzinfo=UTC)


def _positions() -> tuple[str, ...]:
    return (
        "GK",
        "DEF",
        "DEF",
        "DEF",
        "MID",
        "MID",
        "MID",
        "MID",
        "FWD",
        "FWD",
        "FWD",
        "GK",
        "DEF",
        "DEF",
        "MID",
    )


def _database(tmp_path):
    database_path = tmp_path / "fpl.duckdb"
    initialize_database(database_path)
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO ingestion_run (
                ingestion_run_id, source, captured_at, status
            ) VALUES ('official', 'fpl_api', ?, 'completed')
            """,
            [OFFICIAL_CAPTURED_AT],
        )
        connection.execute(
            """
            INSERT INTO gameweek_snapshot VALUES (
                'official', 1, 'Gameweek 1', ?, NULL, FALSE, FALSE, FALSE, FALSE, TRUE
            )
            """,
            [DEADLINE],
        )
        connection.executemany(
            """
            INSERT INTO player_snapshot (
                ingestion_run_id, season, fpl_id, player_code, first_name,
                second_name, web_name, team_id, fpl_position, price, fpl_status
            ) VALUES ('official', '2026-27', ?, ?, 'Test', ?, ?, ?, ?, ?, 'a')
            """,
            [
                (
                    index,
                    1000 + index,
                    f"Player {index}",
                    f"Player {index}",
                    ((index - 1) % 5) + 1,
                    position,
                    (50 + index) / 10,
                )
                for index, position in enumerate(_positions(), start=1)
            ],
        )
    return database_path


def _csv(tmp_path, *, missing_fpl_id: int | None = None):
    frame = pd.DataFrame(
        {
            "fpl_id": list(range(1, 16)),
            "purchase_price": [(50 + index) / 10 for index in range(1, 16)],
            "selling_price": [(50 + index) / 10 for index in range(1, 16)],
            "squad_position": list(range(1, 16)),
            "is_captain": [index == 9 for index in range(1, 16)],
            "is_vice_captain": [index == 5 for index in range(1, 16)],
        }
    )
    if missing_fpl_id is not None:
        frame.loc[0, "fpl_id"] = missing_fpl_id
    path = tmp_path / "squad.csv"
    frame.to_csv(path, index=False)
    return path


def _import(tmp_path, **overrides):
    csv_path = overrides.pop("csv_path", None) or _csv(tmp_path)
    database_path = overrides.pop("database_path", None) or _database(tmp_path)
    arguments = {
        "csv_path": csv_path,
        "entry_id": 123456,
        "entry_name": "Test Entry",
        "season": "2026-27",
        "target_gameweek": 1,
        "source_ingestion_run_id": "official",
        "captured_at": SQUAD_CAPTURED_AT,
        "source_label": "manual my-team export",
        "bank": "0.5",
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
    return import_squad_snapshot_csv(**arguments)


def test_imports_immutable_squad_snapshot_with_official_identity_join(tmp_path):
    result = _import(tmp_path)

    assert result.player_rows == 15
    assert result.team_value_tenths == 5 + sum(range(51, 66))
    with duckdb.connect(str(tmp_path / "fpl.duckdb"), read_only=True) as connection:
        snapshot = connection.execute(
            """
            SELECT entry_id, source_ingestion_run_id, bank_tenths,
                   free_transfers, unlimited_transfers, player_rows, constraint_flags
            FROM squad_snapshot
            """
        ).fetchone()
        players = connection.execute(
            """
            SELECT count(*), sum(CASE WHEN is_captain THEN 1 ELSE 0 END),
                   sum(CASE WHEN is_vice_captain THEN 1 ELSE 0 END)
            FROM squad_snapshot_player
            """
        ).fetchone()
        chips = connection.execute(
            "SELECT chip_name, chip_status FROM squad_chip_state ORDER BY chip_name"
        ).fetchall()

    assert snapshot == (123456, "official", 5, None, True, 15, "")
    assert players == (15, 1, 1)
    assert chips == [
        ("bench_boost", "available"),
        ("free_hit", "available"),
        ("triple_captain", "available"),
        ("wildcard", "available"),
    ]


def test_repeating_identical_manual_snapshot_is_idempotent(tmp_path):
    database_path = _database(tmp_path)
    csv_path = _csv(tmp_path)
    arguments = {
        "csv_path": csv_path,
        "entry_id": 123456,
        "entry_name": "Test Entry",
        "season": "2026-27",
        "target_gameweek": 1,
        "source_ingestion_run_id": "official",
        "captured_at": SQUAD_CAPTURED_AT,
        "source_label": "manual my-team export",
        "bank": "0.5",
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

    first = import_squad_snapshot_csv(**arguments)
    second = import_squad_snapshot_csv(**arguments)

    assert first == second
    with duckdb.connect(str(database_path), read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM squad_snapshot").fetchone()[0] == 1


def test_rejects_player_absent_from_pinned_official_snapshot(tmp_path):
    database_path = _database(tmp_path)
    csv_path = _csv(tmp_path, missing_fpl_id=999)

    with pytest.raises(ValueError, match="absent from source FPL snapshot"):
        _import(tmp_path, csv_path=csv_path, database_path=database_path)


def test_rejects_snapshot_captured_after_target_deadline(tmp_path):
    with pytest.raises(ValueError, match="must not be after"):
        _import(tmp_path, captured_at=datetime(2026, 8, 21, 18, 0, tzinfo=UTC))


def test_rejects_prices_with_more_than_one_decimal_place(tmp_path):
    database_path = _database(tmp_path)
    csv_path = _csv(tmp_path)
    frame = pd.read_csv(csv_path)
    frame.loc[0, "selling_price"] = 5.15
    frame.to_csv(csv_path, index=False)

    with pytest.raises(ValueError, match="at most one decimal"):
        _import(tmp_path, csv_path=csv_path, database_path=database_path)


def test_rejects_chip_period_that_does_not_match_target_half(tmp_path):
    with pytest.raises(ValueError, match="chip_period must be 1"):
        _import(tmp_path, chip_period=2)
