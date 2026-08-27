"""Classify what actually happened to a player in one completed Gameweek.

P0 (`README.md`'s "Production critical path") requires distinguishing
`starter`, `substitute`, `unused substitute`, `not-in-squad`, `unavailable`,
and `not-yet-eligible` OBSERVATIONS -- as opposed to `validation/role_state.py`,
which derives a PROSPECTIVE role state from a projection before a deadline.
This module is the retrospective counterpart: given one completed, FINAL
Gameweek's official outcome (`fpl_event_live_run`/`player_gameweek_stat`), it
classifies why a player recorded 0 minutes, the same way `material_conflict.py`
compares a completed Gameweek's outcome against its own projection.

FPL's `event/{gw}/live` endpoint returns a stats row for every player in the
game, whether they played or not -- but it does NOT expose the 20-man
matchday squad or bench list. That means `played`/`minutes`/`starts` alone
cannot distinguish a genuine unused substitute (named on the bench, not
brought on) from a player who was not selected in the squad at all. Rather
than guess, this module names that ambiguity explicitly
(`UNUSED_SUBSTITUTE_OR_NOT_IN_SQUAD`) instead of picking one of the two and
being wrong for the other half of the time.

What CAN be told apart from data already in this database:

- `STARTER`: started the fixture (``starts >= 1``).
- `SUBSTITUTE`: came on as a substitute and played (``starts == 0``,
  ``minutes > 0``).
- `UNAVAILABLE`: 0 minutes, and the SAME Gameweek's own availability
  resolution had already resolved this player ineligible ahead of the
  deadline (injury, suspension, or another eligibility block).
- `NOT_YET_ELIGIBLE`: 0 minutes, and the player has no `player_snapshot` row
  under the official snapshot this Gameweek's live data used -- not yet a
  registered player in the game at all (a summer signing before their
  registration snapshot, for example).
- `NO_TEAM_FIXTURE`: 0 minutes, and the player's team had zero fixtures in
  this Gameweek (a blank Gameweek) -- unambiguous and distinct from every
  other 0-minute reason.
- `UNUSED_SUBSTITUTE_OR_NOT_IN_SQUAD`: 0 minutes, team had a fixture, player
  was registered, and no eligibility block was resolved -- genuinely
  indistinguishable from the data available; this is the deliberately named
  gap rather than a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import duckdb

STARTER = "starter"
SUBSTITUTE = "substitute"
UNAVAILABLE = "unavailable"
NOT_YET_ELIGIBLE = "not_yet_eligible"
NO_TEAM_FIXTURE = "no_team_fixture"
UNUSED_SUBSTITUTE_OR_NOT_IN_SQUAD = "unused_substitute_or_not_in_squad"

APPEARANCE_OBSERVATIONS = (
    STARTER,
    SUBSTITUTE,
    UNAVAILABLE,
    NOT_YET_ELIGIBLE,
    NO_TEAM_FIXTURE,
    UNUSED_SUBSTITUTE_OR_NOT_IN_SQUAD,
)


@dataclass(frozen=True, slots=True)
class AppearanceObservationResult:
    observation: str
    reason: str

    def __post_init__(self) -> None:
        if self.observation not in APPEARANCE_OBSERVATIONS:
            raise ValueError(f"unknown observation: {self.observation!r}")
        if not self.reason.strip():
            raise ValueError("reason must not be blank")


def derive_appearance_observation(
    *,
    minutes: int,
    starts: int,
    is_eligible: bool | None,
    is_registered: bool,
    team_has_fixture: bool,
) -> AppearanceObservationResult:
    """Classify one player's realised outcome for one completed Gameweek.

    ``is_eligible`` is the resolved availability outcome from the SAME
    Gameweek's own availability resolution (``None`` when unresolved/unknown,
    which is treated the same as ``True`` here -- an unresolved eligibility
    is not itself evidence the player was blocked, only that the pipeline did
    not determine one either way). ``is_registered`` is whether the player has
    a `player_snapshot` row under the official snapshot this Gameweek's live
    data used. ``team_has_fixture`` is whether the player's team had at least
    one fixture in this Gameweek.
    """
    if minutes < 0 or starts < 0:
        raise ValueError("minutes and starts must be non-negative")
    if starts >= 1:
        return AppearanceObservationResult(
            observation=STARTER,
            reason=f"Started and played {minutes} minutes.",
        )
    if minutes > 0:
        return AppearanceObservationResult(
            observation=SUBSTITUTE,
            reason=f"Came on as a substitute and played {minutes} minutes.",
        )
    if not is_registered:
        return AppearanceObservationResult(
            observation=NOT_YET_ELIGIBLE,
            reason="Not yet a registered player in the official snapshot this Gameweek.",
        )
    if is_eligible is False:
        return AppearanceObservationResult(
            observation=UNAVAILABLE,
            reason="Resolved unavailable ahead of the deadline (injury, suspension, or block).",
        )
    if not team_has_fixture:
        return AppearanceObservationResult(
            observation=NO_TEAM_FIXTURE,
            reason="Player's team had no fixture this Gameweek (blank Gameweek).",
        )
    return AppearanceObservationResult(
        observation=UNUSED_SUBSTITUTE_OR_NOT_IN_SQUAD,
        reason=(
            "Played 0 minutes with no resolved eligibility block and a team fixture "
            "played -- FPL's live data does not expose the matchday squad or bench "
            "list, so an unused substitute cannot be told apart from a player left "
            "out of the squad entirely."
        ),
    )


def load_appearance_observations(
    connection: duckdb.DuckDBPyConnection,
    *,
    live_run_id: str,
) -> dict[int, AppearanceObservationResult]:
    """Classify every player's outcome for one FINAL, completed Gameweek.

    ``live_run_id`` must reference a FINAL `fpl_event_live_run`
    (``event_finished AND data_checked``) -- comparing a provisional
    Gameweek would risk classifying a player as `unused_substitute_or_not_in_squad`
    before FPL has finished checking the outcome. Eligibility is resolved from
    the MOST RECENT `availability_resolution_run` targeting the SAME Gameweek
    under the SAME official snapshot as the live run, if one exists; when none
    exists, ``is_eligible`` is treated as unresolved (``None``) for every
    player rather than raising, since a retrospective classification should
    not require a resolution run to have been kept just for this purpose.
    """
    live_run = connection.execute(
        """
        SELECT gameweek, source_ingestion_run_id, event_finished, data_checked
        FROM fpl_event_live_run
        WHERE live_run_id = ?
        """,
        [live_run_id],
    ).fetchone()
    if live_run is None:
        raise ValueError(f"unknown live_run_id: {live_run_id}")
    gameweek, source_ingestion_run_id, event_finished, data_checked = live_run
    if not (event_finished and data_checked):
        raise ValueError(
            f"event-live run {live_run_id} is not final "
            f"(event_finished={event_finished}, data_checked={data_checked})"
        )

    resolution_run = connection.execute(
        """
        SELECT rr.resolution_run_id
        FROM availability_resolution_run AS rr
        WHERE rr.source_ingestion_run_id = ? AND rr.target_gameweek = ?
        ORDER BY rr.as_of DESC, rr.created_at DESC
        LIMIT 1
        """,
        [source_ingestion_run_id, gameweek],
    ).fetchone()
    eligibility_by_fpl_id: dict[int, bool | None] = {}
    if resolution_run is not None:
        eligibility_by_fpl_id = {
            int(fpl_id): (None if is_eligible is None else bool(is_eligible))
            for fpl_id, is_eligible in connection.execute(
                "SELECT fpl_id, is_eligible FROM player_availability_resolution "
                "WHERE resolution_run_id = ?",
                [resolution_run[0]],
            ).fetchall()
        }

    registered_fpl_ids = {
        int(row[0])
        for row in connection.execute(
            "SELECT fpl_id FROM player_snapshot WHERE ingestion_run_id = ?",
            [source_ingestion_run_id],
        ).fetchall()
    }
    team_by_fpl_id = {
        int(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT fpl_id, team_id FROM player_snapshot WHERE ingestion_run_id = ?",
            [source_ingestion_run_id],
        ).fetchall()
    }
    teams_with_fixture = {
        team_id
        for home_id, away_id in connection.execute(
            "SELECT home_team_id, away_team_id FROM fixture_snapshot "
            "WHERE ingestion_run_id = ? AND gameweek = ?",
            [source_ingestion_run_id, gameweek],
        ).fetchall()
        for team_id in (int(home_id), int(away_id))
    }

    rows = connection.execute(
        "SELECT fpl_id, minutes, starts FROM player_gameweek_stat WHERE live_run_id = ?",
        [live_run_id],
    ).fetchall()

    result: dict[int, AppearanceObservationResult] = {}
    for fpl_id, minutes, starts in rows:
        fpl_id = int(fpl_id)
        is_registered = fpl_id in registered_fpl_ids
        team_id = team_by_fpl_id.get(fpl_id)
        team_has_fixture = team_id is not None and team_id in teams_with_fixture
        result[fpl_id] = derive_appearance_observation(
            minutes=int(minutes),
            starts=int(starts),
            is_eligible=eligibility_by_fpl_id.get(fpl_id),
            is_registered=is_registered,
            team_has_fixture=team_has_fixture,
        )
    return result


def appearance_observation_report(
    result: AppearanceObservationResult | None,
) -> dict[str, Any] | None:
    """Render one player's appearance observation as a JSON-serialisable dict, or None."""
    if result is None:
        return None
    return {"observation": result.observation, "reason": result.reason}
