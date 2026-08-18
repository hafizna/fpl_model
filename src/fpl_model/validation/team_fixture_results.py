"""Derive Vaastav-only team-fixture facts for the walk-forward backtest.

Raw ``merged_gw.csv`` rows are player-fixture grain, but every player on a team
shares that team's realised result for a given fixture. This module collapses
player rows into one row per ``(team, fixture)`` using only fields Vaastav
already provides, without touching ``player_fixture_history``'s existing
import contract (which drops the score/xGC columns used here). Nothing here is
persisted to DuckDB; it is a pure, in-memory derivation consumed by
``team_strength_asof.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

REQUIRED_GAMEWEEK_COLUMNS = (
    "element",
    "team",
    "GW",
    "fixture",
    "kickoff_time",
    "was_home",
    "opponent_team",
    "team_a_score",
    "team_h_score",
    "minutes",
    "expected_goals",
    "expected_goals_conceded",
)
REQUIRED_PLAYER_COLUMNS = ("id", "team")


@dataclass(frozen=True, slots=True)
class TeamFixtureResult:
    """One team's realised and provider-expected outcome for one fixture."""

    team: str
    fixture_id: int
    gameweek: int
    kickoff_time: datetime
    was_home: bool
    opponent_team_id: int
    team_goals_for: int
    team_goals_against: int
    team_xg_for: float
    team_xg_against: float
    partial_match_xgc_sample: bool

    def __post_init__(self) -> None:
        if not self.team.strip():
            raise ValueError("team must not be blank")
        if self.fixture_id <= 0:
            raise ValueError("fixture_id must be a positive integer")
        if not 1 <= self.gameweek <= 38:
            raise ValueError("gameweek must be between 1 and 38")
        if self.kickoff_time.tzinfo is None:
            raise ValueError("kickoff_time must be timezone-aware")
        if self.opponent_team_id <= 0:
            raise ValueError("opponent_team_id must be a positive integer")
        for name in ("team_goals_for", "team_goals_against"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        for name in ("team_xg_for", "team_xg_against"):
            if getattr(self, name) < 0.0:
                raise ValueError(f"{name} must be non-negative")


def _require_columns(frame: pd.DataFrame, columns: tuple[str, ...], label: str) -> None:
    missing = set(columns) - set(frame.columns)
    if missing:
        raise ValueError(f"{label} missing columns: {', '.join(sorted(missing))}")


def build_team_fixture_results(gameweeks: pd.DataFrame) -> pd.DataFrame:
    """Derive one row per ``(team, fixture)`` from raw ``merged_gw.csv`` rows.

    ``was_home``, ``opponent_team``, and the team's own/conceded final score are
    validated as constant within each ``(team, fixture)`` group (one Vaastav
    team-fixture fact, not a genuine per-player quantity) and raise on conflict.
    ``team_xg_for`` sums ``expected_goals`` across the team's rows in that
    fixture. ``team_xg_against`` is Vaastav's per-player, minutes-scaled
    ``expected_goals_conceded`` taken from the row(s) at that group's maximum
    minutes -- exactly 90 in the overwhelming case, but a lower value is used as
    a documented proxy (flagged ``partial_match_xgc_sample=True``) when no
    ``minutes == 90`` row exists in the group, rather than fabricating a value
    or discarding the fixture.
    """
    _require_columns(gameweeks, REQUIRED_GAMEWEEK_COLUMNS, "gameweeks")
    if gameweeks.empty:
        raise ValueError("gameweeks must not be empty")

    frame = gameweeks.loc[:, list(REQUIRED_GAMEWEEK_COLUMNS)].copy()
    frame["kickoff_time"] = pd.to_datetime(frame["kickoff_time"], utc=True, errors="coerce")
    if frame["kickoff_time"].isna().any():
        raise ValueError("kickoff_time contains missing or invalid values")
    frame["GW"] = pd.to_numeric(frame["GW"], errors="coerce")
    frame["fixture"] = pd.to_numeric(frame["fixture"], errors="coerce")
    frame["opponent_team"] = pd.to_numeric(frame["opponent_team"], errors="coerce")
    frame["team_a_score"] = pd.to_numeric(frame["team_a_score"], errors="coerce")
    frame["team_h_score"] = pd.to_numeric(frame["team_h_score"], errors="coerce")
    frame["minutes"] = pd.to_numeric(frame["minutes"], errors="coerce")
    frame["expected_goals"] = pd.to_numeric(frame["expected_goals"], errors="coerce")
    frame["expected_goals_conceded"] = pd.to_numeric(
        frame["expected_goals_conceded"], errors="coerce"
    )
    if (
        frame[
            [
                "GW",
                "fixture",
                "opponent_team",
                "team_a_score",
                "team_h_score",
                "minutes",
                "expected_goals",
                "expected_goals_conceded",
            ]
        ]
        .isna()
        .any()
        .any()
    ):
        raise ValueError("gameweeks contains missing or non-numeric required values")
    if not frame["was_home"].isin([True, False]).all():
        try:
            frame["was_home"] = frame["was_home"].astype(str).str.strip().str.lower().map(
                {"true": True, "false": False}
            )
        except (TypeError, ValueError):
            frame["was_home"] = None
        if frame["was_home"].isna().any():
            raise ValueError("was_home must be a boolean")

    rows: list[tuple[object, ...]] = []
    for (team, fixture_id), group in frame.groupby(["team", "fixture"], sort=False):
        for column in ("was_home", "opponent_team", "team_a_score", "team_h_score", "GW"):
            if group[column].nunique(dropna=False) != 1:
                raise ValueError(
                    f"conflicting {column} within team={team!r} fixture={fixture_id!r}"
                )
        was_home = bool(group["was_home"].iloc[0])
        team_goals_for = int(
            group["team_h_score"].iloc[0] if was_home else group["team_a_score"].iloc[0]
        )
        team_goals_against = int(
            group["team_a_score"].iloc[0] if was_home else group["team_h_score"].iloc[0]
        )
        max_minutes = group["minutes"].max()
        xgc_rows = group.loc[group["minutes"] == max_minutes, "expected_goals_conceded"]
        rows.append(
            (
                str(team),
                int(fixture_id),
                int(group["GW"].iloc[0]),
                group["kickoff_time"].iloc[0].to_pydatetime(),
                was_home,
                int(group["opponent_team"].iloc[0]),
                team_goals_for,
                team_goals_against,
                float(group["expected_goals"].sum()),
                float(xgc_rows.mean()),
                bool(max_minutes < 90),
            )
        )

    result = pd.DataFrame(
        rows,
        columns=[
            "team",
            "fixture_id",
            "gameweek",
            "kickoff_time",
            "was_home",
            "opponent_team_id",
            "team_goals_for",
            "team_goals_against",
            "team_xg_for",
            "team_xg_against",
            "partial_match_xgc_sample",
        ],
    )
    return result.sort_values(["gameweek", "fixture_id", "team"]).reset_index(drop=True)


def build_team_name_to_id(
    players_raw: pd.DataFrame,
    gameweeks: pd.DataFrame,
) -> dict[str, int]:
    """Map ``merged_gw.csv`` team-name strings to their integer FPL team id.

    ``players_raw.csv``'s ``team`` column is the player's *current* team id, but
    a player who transferred mid-season has earlier ``merged_gw.csv`` rows
    naming their old club -- joining through ``element``/``id`` alone therefore
    lets a handful of transferred-out players' rows point a name at the wrong
    id. Each name is resolved to the team id backed by the most fixture rows
    (majority vote by row count, not by distinct player count), which is
    robust to a small number of mid-season transfers without needing an
    explicit transfer history. A name with no votes at all (no player row
    joins to a known ``players_raw`` id) is omitted, never guessed.
    """
    _require_columns(players_raw, REQUIRED_PLAYER_COLUMNS, "players_raw")
    _require_columns(gameweeks, ("element", "team"), "gameweeks")
    if players_raw.empty or gameweeks.empty:
        raise ValueError("players_raw and gameweeks must not be empty")

    id_to_team_id = dict(
        zip(
            pd.to_numeric(players_raw["id"], errors="raise"),
            pd.to_numeric(players_raw["team"], errors="raise"),
            strict=True,
        )
    )
    rows = gameweeks.loc[:, ["element", "team"]].copy()
    rows["team_id"] = pd.to_numeric(rows["element"], errors="raise").map(id_to_team_id)
    rows = rows.dropna(subset=["team_id"])
    votes = rows.groupby(["team", "team_id"]).size()

    mapping: dict[str, int] = {}
    for name, name_votes in votes.groupby(level=0):
        winning_team_id = int(name_votes.idxmax()[1])
        mapping[str(name)] = winning_team_id

    id_to_name: dict[int, str] = {}
    for name, team_id in mapping.items():
        existing_name = id_to_name.get(team_id)
        if existing_name is not None and existing_name != name:
            raise ValueError(f"team id {team_id!r} maps to more than one team name")
        id_to_name[team_id] = name
    return mapping
