"""Freshness, fixture-completion, FPL-finality, and drift-eligibility checks.

The release manifest (`validation/release_manifest.py`) proves that a chosen set of
model runs cohere as one linked horizon. It deliberately does not ask whether that
horizon's evidence is still current, whether each Gameweek's fixtures have actually
finished, or whether FPL has marked a Gameweek officially final. This module adds
those checks as a second, independent pass over the same run set.

Like the manifest, this never fails just because evidence is provisional --
provisional evidence is a normal, expected mid-season state (see
`docs/INSEASON_REFRESH.md`). It fails closed only on conditions that make a
release actively unsafe to call fresh: a source snapshot captured after the
deadline it was used to project for (a lookahead hazard; `model_run` itself
already enforces `as_of <= deadline` at the database level, so only the
snapshot's own `captured_at` needs checking here) or a Gameweek entirely absent
from its source snapshot. Everything else -- staleness relative to "now",
incomplete fixtures, non-final FPL state -- is reported as a flag for a human or a
stricter downstream policy to act on, not a hard failure here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from fpl_model.storage import DEFAULT_DATABASE_PATH

DEFAULT_STALE_AFTER_HOURS = 24.0


@dataclass(frozen=True, slots=True)
class ReleaseFreshnessReport:
    report: dict[str, Any]

    @property
    def passes(self) -> bool:
        return bool(self.report["passes"])


def _row(connection: duckdb.DuckDBPyConnection, sql: str, params: list[Any]) -> tuple | None:
    return connection.execute(sql, params).fetchone()


def _gameweek_check(
    connection: duckdb.DuckDBPyConnection,
    *,
    model_run_id: str,
    target_gameweek: int,
    as_of: datetime,
    deadline: datetime,
    source_ingestion_run_id: str,
    now: datetime,
    stale_after_hours: float,
) -> dict[str, Any]:
    problems: list[str] = []
    flags: list[str] = []

    snapshot = _row(
        connection,
        "SELECT captured_at FROM ingestion_run WHERE ingestion_run_id = ?",
        [source_ingestion_run_id],
    )
    if snapshot is None:
        raise ValueError(f"unknown ingestion_run_id: {source_ingestion_run_id}")
    captured_at = snapshot[0]

    # model_run has a DB-level CHECK(as_of <= deadline), so that hazard can never be
    # stored. captured_at is independent of as_of/deadline, so it is not similarly
    # constrained and is checked here.
    if captured_at > deadline:
        problems.append(
            f"GW{target_gameweek}: source snapshot captured_at ({captured_at.isoformat()}) is "
            f"after the deadline ({deadline.isoformat()}) it was used to project -- lookahead hazard"
        )

    deadline_passed = now >= deadline
    snapshot_age_hours = (now - captured_at).total_seconds() / 3600.0
    if not deadline_passed and snapshot_age_hours > stale_after_hours:
        flags.append("SNAPSHOT_STALE_RELATIVE_TO_NOW")

    gameweek_state = _row(
        connection,
        """
        SELECT finished, data_checked
        FROM gameweek_snapshot
        WHERE ingestion_run_id = ? AND gameweek = ?
        """,
        [source_ingestion_run_id, target_gameweek],
    )
    if gameweek_state is None:
        problems.append(
            f"GW{target_gameweek}: source snapshot has no gameweek_snapshot row for this Gameweek"
        )
        fpl_finished = None
        fpl_data_checked = None
    else:
        fpl_finished, fpl_data_checked = gameweek_state
        fpl_finished = bool(fpl_finished)
        fpl_data_checked = bool(fpl_data_checked)
        if not fpl_finished:
            flags.append("GAMEWEEK_NOT_FINISHED")
        if fpl_finished and not fpl_data_checked:
            flags.append("GAMEWEEK_FINISHED_NOT_DATA_CHECKED")

    fixture_counts = _row(
        connection,
        """
        SELECT count(*), count(*) FILTER (WHERE finished)
        FROM fixture_snapshot
        WHERE ingestion_run_id = ? AND gameweek = ?
        """,
        [source_ingestion_run_id, target_gameweek],
    )
    total_fixtures, finished_fixtures = fixture_counts if fixture_counts else (0, 0)
    total_fixtures = int(total_fixtures or 0)
    finished_fixtures = int(finished_fixtures or 0)
    if total_fixtures == 0:
        problems.append(
            f"GW{target_gameweek}: source snapshot has no fixture_snapshot rows for this Gameweek"
        )
        analytically_complete = False
    else:
        analytically_complete = finished_fixtures == total_fixtures
        if not analytically_complete:
            flags.append("FIXTURES_INCOMPLETE")

    is_final = bool(fpl_finished) and bool(fpl_data_checked)
    drift_check_eligible = analytically_complete and not is_final
    if drift_check_eligible:
        flags.append("PROVISIONAL_DRIFT_CHECK_ELIGIBLE")

    return {
        "target_gameweek": target_gameweek,
        "model_run_id": model_run_id,
        "source_ingestion_run_id": source_ingestion_run_id,
        "as_of": as_of.isoformat(),
        "deadline": deadline.isoformat(),
        "snapshot_captured_at": captured_at.isoformat(),
        "snapshot_age_hours_relative_to_now": round(snapshot_age_hours, 2),
        "deadline_passed_relative_to_now": deadline_passed,
        "fixtures": {
            "total": total_fixtures,
            "finished": finished_fixtures,
            "analytically_complete": analytically_complete,
        },
        "fpl_finality": {
            "finished": fpl_finished,
            "data_checked": fpl_data_checked,
            "is_final": is_final,
        },
        "drift_check_eligible": drift_check_eligible,
        "flags": flags,
        "problems": problems,
    }


def check_release_freshness(
    *,
    model_run_ids: tuple[str, ...],
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    now: datetime | None = None,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
) -> ReleaseFreshnessReport:
    """Check freshness, fixture-completion, and FPL-finality for a set of model runs.

    This is deliberately a second, independent pass alongside
    `validation.release_manifest.build_release_manifest` rather than folded into
    it -- see that module's own note that freshness/finality/drift are separate
    Sprint 5 gates. `now` defaults to the real current time and is only ever
    overridden for reproducible testing.
    """
    if not model_run_ids:
        raise ValueError("check_release_freshness requires at least one model_run_id")
    if len(set(model_run_ids)) != len(model_run_ids):
        raise ValueError("model_run_ids contains duplicates")
    if stale_after_hours <= 0:
        raise ValueError("stale_after_hours must be positive")
    reference_time = now or datetime.now(UTC)

    with duckdb.connect(str(database_path), read_only=True) as connection:
        gameweeks: list[dict[str, Any]] = []
        for model_run_id in model_run_ids:
            row = _row(
                connection,
                """
                SELECT target_gameweek, as_of, deadline, source_ingestion_run_id
                FROM model_run
                WHERE model_run_id = ?
                """,
                [model_run_id],
            )
            if row is None:
                raise ValueError(f"unknown model_run_id: {model_run_id}")
            target_gameweek, as_of, deadline, source_ingestion_run_id = row
            gameweeks.append(
                _gameweek_check(
                    connection,
                    model_run_id=model_run_id,
                    target_gameweek=int(target_gameweek),
                    as_of=as_of,
                    deadline=deadline,
                    source_ingestion_run_id=str(source_ingestion_run_id),
                    now=reference_time,
                    stale_after_hours=stale_after_hours,
                )
            )

    all_problems = [problem for gw in gameweeks for problem in gw["problems"]]
    passes = not all_problems

    payload: dict[str, Any] = {
        "label": "release_freshness_v1",
        "model_run_ids": list(model_run_ids),
        "checked_at": reference_time.isoformat(),
        "stale_after_hours": stale_after_hours,
        "passes": passes,
        "problems": all_problems,
        "gameweeks": gameweeks,
        "limitations": [
            "This check reports staleness, incompleteness, and non-finality as flags; it "
            "does not fail on them. It only fails closed on a lookahead hazard (source "
            "snapshot captured after the deadline it was used to project for) or a "
            "Gameweek entirely absent from the source snapshot.",
            "drift_check_eligible marks a Gameweek whose fixtures are all analytically "
            "complete but FPL has not yet marked finished+data_checked -- i.e. eligible for "
            "a future provisional-to-final rebuild-and-compare, not evidence that one has "
            "been run.",
            "SNAPSHOT_STALE_RELATIVE_TO_NOW compares the snapshot to the current wall clock "
            "only when the Gameweek's deadline has not yet passed; a completed Gameweek's "
            "snapshot age relative to now is not itself informative.",
        ],
    }
    return ReleaseFreshnessReport(report=payload)
