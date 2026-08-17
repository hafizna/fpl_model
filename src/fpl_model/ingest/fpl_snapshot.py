"""Persist timestamped official FPL API snapshots without overwriting history."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from fpl_model.ingest.fpl import POSITION_MAP
from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database

DEFAULT_RAW_ROOT = Path("data/raw/fpl")


@dataclass(frozen=True, slots=True)
class FPLSnapshotResult:
    ingestion_run_id: str
    captured_at: datetime
    payload_sha256: str
    manifest_path: Path
    players: int
    teams: int
    gameweeks: int
    fixtures: int


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("captured_at must be timezone-aware")


def _required_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"bootstrap field {key!r} must be a non-empty list")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError(f"bootstrap field {key!r} contains a non-object item")
    return value


def _number(value: object, field: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"FPL field {field!r} is not numeric: {value!r}") from error


def _integer(value: object, field: str) -> int:
    number = _number(value, field)
    assert number is not None
    integer = int(number)
    if number != integer:
        raise ValueError(f"FPL field {field!r} is not an integer: {value!r}")
    return integer


def _unique(rows: list[dict[str, Any]], key: str, label: str) -> None:
    values = [_integer(row.get(key), f"{label}.{key}") for row in rows]
    if len(values) != len(set(values)):
        raise ValueError(f"{label} contains duplicate {key} values")


def _validate_snapshot(
    bootstrap: dict[str, Any],
    fixtures: list[dict[str, Any]],
    captured_at: datetime,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    _require_aware(captured_at)
    players = _required_list(bootstrap, "elements")
    teams = _required_list(bootstrap, "teams")
    gameweeks = _required_list(bootstrap, "events")
    if not isinstance(fixtures, list) or not all(isinstance(item, dict) for item in fixtures):
        raise ValueError("fixtures must be a list of objects")

    _unique(players, "id", "players")
    _unique(teams, "id", "teams")
    _unique(gameweeks, "id", "events")
    _unique(fixtures, "id", "fixtures")

    team_ids = {_integer(row.get("id"), "teams.id") for row in teams}
    event_ids = {_integer(row.get("id"), "events.id") for row in gameweeks}
    for player in players:
        if _integer(player.get("team"), "elements.team") not in team_ids:
            raise ValueError("player references an unknown team")
        element_type = _integer(player.get("element_type"), "elements.element_type")
        if element_type not in POSITION_MAP:
            raise ValueError(f"unknown FPL element_type: {element_type}")
    for fixture in fixtures:
        if _integer(fixture.get("team_h"), "fixtures.team_h") not in team_ids:
            raise ValueError("fixture references an unknown home team")
        if _integer(fixture.get("team_a"), "fixtures.team_a") not in team_ids:
            raise ValueError("fixture references an unknown away team")
        event = fixture.get("event")
        if event is not None and _integer(event, "fixtures.event") not in event_ids:
            raise ValueError("fixture references an unknown gameweek")

    return players, teams, gameweeks


def _snapshot_hash(bootstrap: dict[str, Any], fixtures: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(_canonical_json(bootstrap))
    digest.update(b"\n")
    digest.update(_canonical_json(fixtures))
    return digest.hexdigest()


def _run_id(captured_at: datetime, payload_sha256: str) -> str:
    timestamp = captured_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"fpl_{timestamp}_{payload_sha256[:12]}"


def _write_raw_payload(
    *,
    raw_root: Path,
    run_id: str,
    captured_at: datetime,
    payload_sha256: str,
    bootstrap: dict[str, Any],
    fixtures: list[dict[str, Any]],
) -> Path:
    directory = raw_root / run_id
    directory.mkdir(parents=True, exist_ok=True)
    bootstrap_path = directory / "bootstrap-static.json"
    fixtures_path = directory / "fixtures.json"
    manifest_path = directory / "manifest.json"

    bootstrap_path.write_bytes(_canonical_json(bootstrap) + b"\n")
    fixtures_path.write_bytes(_canonical_json(fixtures) + b"\n")
    manifest = {
        "ingestion_run_id": run_id,
        "captured_at": captured_at.isoformat(),
        "payload_sha256": payload_sha256,
        "bootstrap_file": bootstrap_path.name,
        "fixtures_file": fixtures_path.name,
        "source": "official_fpl_api",
    }
    manifest_path.write_bytes(_canonical_json(manifest) + b"\n")
    return manifest_path.resolve()


def _player_rows(run_id: str, players: list[dict[str, Any]]) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for player in players:
        element_type = _integer(player.get("element_type"), "elements.element_type")
        rows.append(
            (
                run_id,
                _integer(player.get("id"), "elements.id"),
                player.get("code"),
                str(player.get("first_name") or ""),
                str(player.get("second_name") or ""),
                str(player.get("web_name") or ""),
                _integer(player.get("team"), "elements.team"),
                POSITION_MAP[element_type],
                _number(player.get("now_cost"), "elements.now_cost") / 10.0,
                str(player.get("status") or ""),
                player.get("chance_of_playing_this_round"),
                player.get("chance_of_playing_next_round"),
                str(player.get("news") or ""),
                player.get("news_added"),
            )
        )
    return rows


def _status_rows(run_id: str, players: list[dict[str, Any]]) -> list[tuple[object, ...]]:
    return [
        (
            run_id,
            _integer(player.get("id"), "elements.id"),
            bool(player.get("can_select")),
            bool(player.get("can_transact")),
            bool(player.get("removed")),
            _number(player.get("selected_by_percent"), "elements.selected_by_percent"),
            _integer(player.get("transfers_in"), "elements.transfers_in"),
            _integer(player.get("transfers_in_event"), "elements.transfers_in_event"),
            _integer(player.get("transfers_out"), "elements.transfers_out"),
            _integer(player.get("transfers_out_event"), "elements.transfers_out_event"),
            _integer(player.get("event_points"), "elements.event_points"),
            _integer(player.get("total_points"), "elements.total_points"),
            _number(player.get("form"), "elements.form"),
            _number(player.get("ep_this"), "elements.ep_this", optional=True),
            _number(player.get("ep_next"), "elements.ep_next", optional=True),
            player.get("team_join_date"),
        )
        for player in players
    ]


def _stat_rows(run_id: str, players: list[dict[str, Any]]) -> list[tuple[object, ...]]:
    integer_fields = (
        "minutes",
        "starts",
        "goals_scored",
        "assists",
        "clean_sheets",
        "goals_conceded",
        "saves",
        "yellow_cards",
        "red_cards",
        "bonus",
        "bps",
        "own_goals",
        "penalties_saved",
        "penalties_missed",
        "defensive_contribution",
        "clearances_blocks_interceptions",
        "recoveries",
        "tackles",
    )
    expected_fields = (
        "expected_goals",
        "expected_assists",
        "expected_goal_involvements",
        "expected_goals_conceded",
    )
    return [
        (
            run_id,
            _integer(player.get("id"), "elements.id"),
            *(
                _integer(player.get(field), f"elements.{field}")
                for field in integer_fields
            ),
            *(
                _number(player.get(field), f"elements.{field}")
                for field in expected_fields
            ),
        )
        for player in players
    ]


def persist_fpl_snapshot(
    *,
    bootstrap: dict[str, Any],
    fixtures: list[dict[str, Any]],
    captured_at: datetime,
    season: str,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    raw_root: str | Path = DEFAULT_RAW_ROOT,
) -> FPLSnapshotResult:
    """Validate, archive, and transactionally persist one official FPL snapshot."""
    players, teams, gameweeks = _validate_snapshot(bootstrap, fixtures, captured_at)
    payload_sha256 = _snapshot_hash(bootstrap, fixtures)
    run_id = _run_id(captured_at, payload_sha256)
    initialize_database(database_path)

    player_rows = [
        (run_id, season, *row[1:]) for row in _player_rows(run_id, players)
    ]
    status_rows = _status_rows(run_id, players)
    stat_rows = _stat_rows(run_id, players)
    team_rows = [
        (
            run_id,
            _integer(team.get("id"), "teams.id"),
            _integer(team.get("code"), "teams.code"),
            str(team.get("name") or ""),
            str(team.get("short_name") or ""),
            bool(team.get("unavailable")),
            _number(team.get("strength"), "teams.strength", optional=True),
            _number(
                team.get("strength_overall_home"),
                "teams.strength_overall_home",
                optional=True,
            ),
            _number(
                team.get("strength_overall_away"),
                "teams.strength_overall_away",
                optional=True,
            ),
            _number(
                team.get("strength_attack_home"),
                "teams.strength_attack_home",
                optional=True,
            ),
            _number(
                team.get("strength_attack_away"),
                "teams.strength_attack_away",
                optional=True,
            ),
            _number(
                team.get("strength_defence_home"),
                "teams.strength_defence_home",
                optional=True,
            ),
            _number(
                team.get("strength_defence_away"),
                "teams.strength_defence_away",
                optional=True,
            ),
        )
        for team in teams
    ]
    gameweek_rows = [
        (
            run_id,
            _integer(event.get("id"), "events.id"),
            str(event.get("name") or ""),
            event.get("deadline_time"),
            event.get("release_time"),
            bool(event.get("finished")),
            bool(event.get("data_checked")),
            bool(event.get("is_previous")),
            bool(event.get("is_current")),
            bool(event.get("is_next")),
        )
        for event in gameweeks
    ]
    fixture_rows = [
        (
            run_id,
            _integer(fixture.get("id"), "fixtures.id"),
            fixture.get("event"),
            fixture.get("kickoff_time"),
            _integer(fixture.get("team_h"), "fixtures.team_h"),
            _integer(fixture.get("team_a"), "fixtures.team_a"),
            bool(fixture.get("started")),
            bool(fixture.get("finished")),
        )
        for fixture in fixtures
    ]
    manifest_path = _write_raw_payload(
        raw_root=Path(raw_root),
        run_id=run_id,
        captured_at=captured_at,
        payload_sha256=payload_sha256,
        bootstrap=bootstrap,
        fixtures=fixtures,
    )

    with duckdb.connect(str(database_path)) as connection:
        existing = connection.execute(
            "SELECT payload_sha256 FROM ingestion_run WHERE ingestion_run_id = ?",
            [run_id],
        ).fetchone()
        if existing is not None:
            if existing[0] != payload_sha256:
                raise ValueError(f"ingestion run ID collision: {run_id}")
            return FPLSnapshotResult(
                run_id,
                captured_at,
                payload_sha256,
                manifest_path,
                len(players),
                len(teams),
                len(gameweeks),
                len(fixtures),
            )

        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute(
                """
                INSERT INTO ingestion_run (
                    ingestion_run_id, source, captured_at, source_as_of,
                    completed_at, status, raw_payload_path, payload_sha256
                ) VALUES (?, 'official_fpl_api', ?, ?, ?, 'completed', ?, ?)
                """,
                [
                    run_id,
                    captured_at,
                    captured_at,
                    captured_at,
                    str(manifest_path),
                    payload_sha256,
                ],
            )
            connection.executemany(
                """
                INSERT INTO player_snapshot VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                player_rows,
            )
            connection.executemany(
                "INSERT INTO player_status_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                status_rows,
            )
            connection.executemany(
                "INSERT INTO player_season_stat_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                stat_rows,
            )
            connection.executemany(
                "INSERT INTO team_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                team_rows,
            )
            connection.executemany(
                "INSERT INTO gameweek_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                gameweek_rows,
            )
            connection.executemany(
                "INSERT INTO fixture_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                fixture_rows,
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    return FPLSnapshotResult(
        run_id,
        captured_at,
        payload_sha256,
        manifest_path,
        len(players),
        len(teams),
        len(gameweeks),
        len(fixtures),
    )
