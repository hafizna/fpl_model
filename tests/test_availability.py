from __future__ import annotations

from datetime import UTC, datetime, timedelta

import duckdb
import pytest

from fpl_model.context.availability import (
    AvailabilityInput,
    ReviewedAvailabilityOverride,
    materialize_latest_fpl_availability,
    resolve_availability,
)
from fpl_model.storage import initialize_database

AS_OF = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)


def _player(**overrides):
    values = {
        "fpl_id": 1,
        "player_code": 1001,
        "fpl_status": "a",
        "official_chance": None,
        "chance_horizon_available": True,
        "can_select": True,
        "removed": False,
    }
    values.update(overrides)
    return AvailabilityInput(**values)


def test_available_blank_chance_resolves_to_one_without_parsing_news():
    result = resolve_availability(
        _player(news="Minor knock but expected to train"),
        as_of=AS_OF,
        target_gameweek=1,
    )

    assert result.availability_probability == 1.0
    assert result.is_eligible is True
    assert result.selected_source == "official_fpl_status"
    assert "FPL_NEWS_WITHOUT_TIMESTAMP" in result.data_quality_flags


def test_doubtful_blank_chance_remains_unresolved():
    result = resolve_availability(
        _player(fpl_status="d"),
        as_of=AS_OF,
        target_gameweek=1,
    )

    assert result.availability_probability is None
    assert result.is_eligible is True
    assert result.selected_source == "unresolved"
    assert "MISSING_AVAILABILITY_PROBABILITY" in result.data_quality_flags


def test_official_suspension_is_hard_block_even_with_conflicting_chance():
    result = resolve_availability(
        _player(fpl_status="s", official_chance=100),
        as_of=AS_OF,
        target_gameweek=1,
    )

    assert result.availability_probability == 0.0
    assert result.is_eligible is False
    assert "FPL_STATUS_CHANCE_CONFLICT" in result.data_quality_flags


def test_reviewed_override_can_replace_stale_official_injury_probability():
    override = ReviewedAvailabilityOverride(
        override_id="review-1",
        player_code=1001,
        target_gameweek=1,
        observed_at=AS_OF - timedelta(hours=1),
        source="club_press_conference_review",
        rationale="Manager confirmed player trained and is available.",
        availability_probability=0.6,
        is_eligible=True,
    )

    result = resolve_availability(
        _player(fpl_status="i", official_chance=0),
        as_of=AS_OF,
        target_gameweek=1,
        override=override,
    )

    assert result.availability_probability == pytest.approx(0.6)
    assert result.is_eligible is True
    assert result.selected_source == "reviewed_override"
    assert result.selected_override_id == "review-1"


def test_future_override_is_rejected_as_lookahead():
    override = ReviewedAvailabilityOverride(
        override_id="future",
        player_code=1001,
        target_gameweek=1,
        observed_at=AS_OF + timedelta(minutes=1),
        source="manual_review",
        rationale="Arrived after the snapshot.",
        availability_probability=1.0,
    )

    with pytest.raises(ValueError, match="after as_of"):
        resolve_availability(
            _player(),
            as_of=AS_OF,
            target_gameweek=1,
            override=override,
        )


def _insert_snapshot(
    connection: duckdb.DuckDBPyConnection,
    *,
    run_id: str,
    captured_at: datetime,
    deadline: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO ingestion_run (
            ingestion_run_id, source, captured_at, completed_at, status
        ) VALUES (?, 'official_fpl_api', ?, ?, 'completed')
        """,
        [run_id, captured_at, captured_at],
    )
    connection.execute(
        """
        INSERT INTO gameweek_snapshot VALUES (
            ?, 1, 'Gameweek 1', ?, NULL, false, false, false, false, true
        )
        """,
        [run_id, deadline],
    )
    players = [
        (1, 1001, "Available", "a", None),
        (2, 1002, "Doubt", "d", None),
    ]
    for fpl_id, player_code, name, status, chance in players:
        connection.execute(
            """
            INSERT INTO player_snapshot VALUES (
                ?, '2026-27', ?, ?, '', '', ?, 1, 'MID', 7.5, ?, NULL, ?, '', NULL
            )
            """,
            [run_id, fpl_id, player_code, name, status, chance],
        )
        connection.execute(
            """
            INSERT INTO player_status_snapshot VALUES (
                ?, ?, true, true, false, 0, 0, 0, 0, 0, 0, 0, 0,
                NULL, NULL, NULL
            )
            """,
            [run_id, fpl_id],
        )


def test_materializer_uses_latest_causal_snapshot_and_reviewed_override(tmp_path):
    database_path = tmp_path / "fpl.duckdb"
    initialize_database(database_path)
    deadline = datetime(2026, 8, 22, 17, 30, tzinfo=UTC)
    with duckdb.connect(str(database_path)) as connection:
        _insert_snapshot(
            connection,
            run_id="causal",
            captured_at=AS_OF,
            deadline=deadline,
        )
        _insert_snapshot(
            connection,
            run_id="after-deadline",
            captured_at=deadline + timedelta(minutes=1),
            deadline=deadline,
        )
        connection.execute(
            """
            INSERT INTO availability_override VALUES (
                'review-doubt', 1002, 1, ?, NULL, 0.75, true,
                'manual_review', 'Reviewed before deadline'
            )
            """,
            [AS_OF - timedelta(minutes=5)],
        )

    result = materialize_latest_fpl_availability(
        target_gameweek=1,
        database_path=database_path,
    )

    assert result.source_ingestion_run_id == "causal"
    assert result.players == 2
    assert result.resolved_players == 2
    assert result.unresolved_players == 0
    assert result.status == "completed"
    with duckdb.connect(str(database_path), read_only=True) as connection:
        rows = connection.execute(
            """
            SELECT player_code, availability_probability, selected_source
            FROM player_availability_resolution
            ORDER BY player_code
            """
        ).fetchall()
    assert rows == [
        (1001, 1.0, "official_fpl_status"),
        (1002, 0.75, "reviewed_override"),
    ]


def test_materializer_records_unresolved_gap_without_inventing_probability(tmp_path):
    database_path = tmp_path / "fpl.duckdb"
    initialize_database(database_path)
    deadline = datetime(2026, 8, 22, 17, 30, tzinfo=UTC)
    with duckdb.connect(str(database_path)) as connection:
        _insert_snapshot(
            connection,
            run_id="causal",
            captured_at=AS_OF,
            deadline=deadline,
        )

    result = materialize_latest_fpl_availability(
        target_gameweek=1,
        database_path=database_path,
    )

    assert result.unresolved_players == 1
    assert result.status == "completed_with_gaps"
    with duckdb.connect(str(database_path), read_only=True) as connection:
        probability, flags = connection.execute(
            """
            SELECT availability_probability, data_quality_flags
            FROM player_availability_resolution
            WHERE player_code = 1002
            """
        ).fetchone()
    assert probability is None
    assert "MISSING_AVAILABILITY_PROBABILITY" in flags
