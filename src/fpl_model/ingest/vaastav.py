"""Adapter for the Vaastav Fantasy-Premier-League upstream repository."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from io import StringIO

import pandas as pd
import requests

RAW_BASE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
RAW_REPOSITORY_BASE = (
    "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League"
)
GITHUB_COMMITS_URL = (
    "https://api.github.com/repos/vaastav/Fantasy-Premier-League/commits"
)


@dataclass(frozen=True, slots=True)
class VaastavRevision:
    """One repository revision that touched a historical data file."""

    sha: str
    committed_at: datetime

    def __post_init__(self) -> None:
        if not self.sha.strip():
            raise ValueError("sha must not be blank")
        if self.committed_at.tzinfo is None or self.committed_at.utcoffset() is None:
            raise ValueError("committed_at must be timezone-aware")


@dataclass(slots=True)
class VaastavClient:
    raw_base: str = RAW_BASE
    timeout_seconds: float = 60.0
    session: requests.Session = field(default_factory=requests.Session, repr=False)

    def _csv(self, relative_path: str) -> pd.DataFrame:
        url = f"{self.raw_base.rstrip('/')}/{relative_path.lstrip('/')}"
        response = self.session.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        return pd.read_csv(StringIO(response.text))

    def file_revisions(
        self,
        season: str,
        *,
        filename: str = "players_raw.csv",
    ) -> tuple[VaastavRevision, ...]:
        """Return commits touching one season file, oldest first."""
        relative_path = f"data/{season}/{filename}"
        response = self.session.get(
            GITHUB_COMMITS_URL,
            timeout=self.timeout_seconds,
            params={"path": relative_path, "per_page": 100},
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "fpl-model/0.1 research client",
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("GitHub commits payload changed shape")

        revisions: list[VaastavRevision] = []
        for item in payload:
            try:
                sha = item["sha"]
                timestamp = item["commit"]["committer"]["date"]
            except (KeyError, TypeError) as error:
                raise ValueError("GitHub commit entry changed shape") from error
            committed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            revisions.append(
                VaastavRevision(
                    sha=str(sha),
                    committed_at=committed_at,
                )
            )
        revisions.sort(key=lambda item: (item.committed_at, item.sha))
        return tuple(revisions)

    def csv_at_revision(
        self,
        season: str,
        filename: str,
        revision: VaastavRevision,
    ) -> pd.DataFrame:
        """Read one season CSV exactly as stored at a pinned commit."""
        url = (
            f"{RAW_REPOSITORY_BASE}/{revision.sha}/data/"
            f"{season}/{filename.lstrip('/')}"
        )
        response = self.session.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        return pd.read_csv(StringIO(response.text))

    def cleaned_players(self, season: str) -> pd.DataFrame:
        return self._csv(f"{season}/cleaned_players.csv")

    def players_raw(self, season: str) -> pd.DataFrame:
        return self._csv(f"{season}/players_raw.csv")

    def fixtures(self, season: str) -> pd.DataFrame:
        return self._csv(f"{season}/fixtures.csv")

    def teams(self, season: str) -> pd.DataFrame:
        return self._csv(f"{season}/teams.csv")

    def player_id_list(self, season: str) -> pd.DataFrame:
        return self._csv(f"{season}/player_idlist.csv")

    def merged_gameweeks(self, season: str) -> pd.DataFrame:
        """Load the season's merged gameweek file when upstream provides it."""
        candidates = [
            f"{season}/gws/merged_gw.csv",
            f"{season}/gws/merged_gws.csv",
        ]
        errors: list[str] = []
        for path in candidates:
            try:
                return self._csv(path)
            except requests.HTTPError as exc:
                errors.append(f"{path}: {exc}")
        raise FileNotFoundError(
            "Could not find a merged gameweek file. Tried: " + "; ".join(errors)
        )

    def world_cup_2026(self) -> pd.DataFrame:
        return self._csv("world_cup_2026.csv")


def latest_revision_at_or_before(
    revisions: tuple[VaastavRevision, ...],
    cutoff: datetime,
) -> VaastavRevision | None:
    """Choose the newest repository snapshot that existed by ``cutoff``."""
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be timezone-aware")
    eligible = tuple(
        revision for revision in revisions if revision.committed_at <= cutoff
    )
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item.committed_at, item.sha))


def add_season_column(frame: pd.DataFrame, season: str) -> pd.DataFrame:
    """Return a copy tagged with season without mutating the upstream frame."""
    result = frame.copy()
    result.insert(0, "season", season)
    return result
