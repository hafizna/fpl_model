from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from fpl_model.ingest.vaastav import (
    VaastavClient,
    latest_revision_at_or_before,
)


@dataclass
class DummyResponse:
    payload: Any = None
    text: str = ""

    def json(self) -> Any:
        return self.payload

    def raise_for_status(self) -> None:
        return None


class DummySession:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> DummyResponse:
        self.calls.append((url, kwargs))
        if "api.github.com" in url:
            return DummyResponse(
                payload=[
                    {
                        "sha": "newer",
                        "commit": {"committer": {"date": "2025-08-20T08:00:00Z"}},
                    },
                    {
                        "sha": "older",
                        "commit": {"committer": {"date": "2025-08-01T08:00:00Z"}},
                    },
                ]
            )
        return DummyResponse(text="id,code\n1,154561\n")


def test_revision_history_is_sorted_and_uses_path_filter():
    session = DummySession()
    client = VaastavClient(session=session)  # type: ignore[arg-type]

    revisions = client.file_revisions("2025-26")

    assert [item.sha for item in revisions] == ["older", "newer"]
    url, kwargs = session.calls[0]
    assert "api.github.com" in url
    assert kwargs["params"]["path"] == "data/2025-26/players_raw.csv"


def test_latest_revision_never_selects_a_future_commit():
    client = VaastavClient(session=DummySession())  # type: ignore[arg-type]
    revisions = client.file_revisions("2025-26")

    selected = latest_revision_at_or_before(
        revisions,
        datetime(2025, 8, 15, tzinfo=UTC),
    )

    assert selected is not None
    assert selected.sha == "older"
    assert (
        latest_revision_at_or_before(
            revisions,
            datetime(2025, 7, 1, tzinfo=UTC),
        )
        is None
    )


def test_reads_snapshot_from_pinned_revision_url():
    session = DummySession()
    client = VaastavClient(session=session)  # type: ignore[arg-type]
    revision = client.file_revisions("2025-26")[0]

    frame = client.csv_at_revision("2025-26", "players_raw.csv", revision)

    assert frame.loc[0, "code"] == 154561
    assert f"/{revision.sha}/data/2025-26/players_raw.csv" in session.calls[-1][0]


def test_revision_selection_rejects_naive_cutoff():
    client = VaastavClient(session=DummySession())  # type: ignore[arg-type]
    revisions = client.file_revisions("2025-26")

    with pytest.raises(ValueError, match="timezone-aware"):
        latest_revision_at_or_before(
            revisions,
            datetime(2025, 8, 15),
        )
