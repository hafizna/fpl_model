from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import run_frozen_appearance_calibration_confirmatory_2026_27 as wrapper  # noqa: E402

from tests.test_appearance_policy_backtest import _import  # noqa: E402

# ---------------------------------------------------------------------------
# Guard Requirement 4 (corrected direction, STRICT inequality): predates-
# deadline (not predates-archive), and a commit exactly AT the deadline is
# rejected -- the protocol requires the freeze commit to PREDATE the
# deadline, and a commit at exactly that instant has not predated it.
# ---------------------------------------------------------------------------


def test_protocol_committed_before_deadline_is_accepted():
    deadline = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    committed_at = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
    assert wrapper.check_protocol_predates_confirmatory_deadline(
        protocol_committed_at=committed_at, deadline=deadline
    )


def test_protocol_committed_after_deadline_is_rejected():
    # This is the corrected direction: a protocol committed AFTER its own
    # confirmatory deadline must be rejected -- not one that predates the
    # season archive (predating the archive is the correct, intended
    # condition and must never itself cause a rejection).
    deadline = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    committed_at = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
    assert not wrapper.check_protocol_predates_confirmatory_deadline(
        protocol_committed_at=committed_at, deadline=deadline
    )


def test_protocol_committed_exactly_at_deadline_is_rejected():
    # Strict inequality: the protocol requires the freeze commit to PREDATE
    # the deadline. A commit timestamped EXACTLY at the deadline has not
    # predated it, so it must be rejected, not accepted.
    deadline = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    assert not wrapper.check_protocol_predates_confirmatory_deadline(
        protocol_committed_at=deadline, deadline=deadline
    )


def test_protocol_committed_one_microsecond_before_deadline_is_accepted():
    deadline = datetime(2026, 8, 25, 10, 0, 0, 0, tzinfo=UTC)
    committed_at = datetime(2026, 8, 25, 9, 59, 59, 999_999, tzinfo=UTC)
    assert wrapper.check_protocol_predates_confirmatory_deadline(
        protocol_committed_at=committed_at, deadline=deadline
    )


def test_protocol_predating_the_archive_by_a_large_margin_is_never_rejected():
    # A protocol committed long before the season even started (i.e. long
    # before the archived data exists) must NOT be rejected on that basis --
    # only being committed AT OR AFTER the deadline is disqualifying.
    deadline = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
    committed_at = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
    assert wrapper.check_protocol_predates_confirmatory_deadline(
        protocol_committed_at=committed_at, deadline=deadline
    )


# ---------------------------------------------------------------------------
# Guard Requirements 1-3: tracked/clean, ancestry, blob-vs-ORIGINAL-addition-
# commit -- via a real, throwaway git repository fixture (never the actual
# project repository).
# ---------------------------------------------------------------------------


def _init_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo_dir, check=True)


def _commit_all(repo_dir: Path, message: str, *, committed_at: str | None = None) -> str:
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)
    env = None
    if committed_at:
        env = {**os.environ, "GIT_AUTHOR_DATE": committed_at, "GIT_COMMITTER_DATE": committed_at}
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo_dir, check=True, env=env)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture
def isolated_repo(tmp_path, monkeypatch):
    """A throwaway git repository the guard functions run against instead of the real repo."""
    repo_dir = tmp_path / "repo"
    _init_repo(repo_dir)
    monkeypatch.setattr(wrapper, "REPO_ROOT", repo_dir)
    return repo_dir


_PROTOCOL_REPO_PATH = Path("docs/research/FROZEN_APPEARANCE_CALIBRATION_POLICY_2026_27.md")


def _write_protocol(repo_dir: Path, text: str = "frozen protocol contents\n") -> Path:
    protocol_dir = repo_dir / "docs" / "research"
    protocol_dir.mkdir(parents=True, exist_ok=True)
    protocol_path = protocol_dir / "FROZEN_APPEARANCE_CALIBRATION_POLICY_2026_27.md"
    protocol_path.write_text(text, encoding="utf-8", newline="\n")
    return protocol_path


def test_guard_rejects_an_untracked_protocol_file(isolated_repo):
    _write_protocol(isolated_repo)
    with pytest.raises(wrapper.GuardFailure, match="not tracked"):
        wrapper.check_protocol_tracked_ancestry_and_blob(protocol_path=_PROTOCOL_REPO_PATH)


def test_guard_rejects_a_committed_protocol_with_uncommitted_edits(isolated_repo):
    protocol_path = _write_protocol(isolated_repo)
    _commit_all(isolated_repo, "freeze")
    # Need SOME later commit to exist as HEAD/run_commit.
    (isolated_repo / "other.txt").write_text("x", encoding="utf-8")
    _commit_all(isolated_repo, "unrelated")

    protocol_path.write_text("edited after freeze\n", encoding="utf-8", newline="\n")

    result = wrapper.check_protocol_tracked_ancestry_and_blob(protocol_path=_PROTOCOL_REPO_PATH)
    assert not result.tracked_and_clean
    assert not result.passed


def test_guard_accepts_a_cleanly_committed_protocol_that_is_an_ancestor_of_head(isolated_repo):
    _write_protocol(isolated_repo)
    protocol_commit = _commit_all(isolated_repo, "freeze protocol")
    (isolated_repo / "other.txt").write_text("later work\n", encoding="utf-8")
    run_commit = _commit_all(isolated_repo, "later work")

    result = wrapper.check_protocol_tracked_ancestry_and_blob(protocol_path=_PROTOCOL_REPO_PATH)

    assert result.protocol_commit == protocol_commit
    assert result.run_commit == run_commit
    assert result.tracked_and_clean
    assert result.is_ancestor_of_run_commit
    assert result.blob_matches_working_tree
    assert result.passed


def test_guard_pins_the_original_addition_commit_not_the_latest_touching_commit(isolated_repo):
    # This is the corrected behaviour: an edited-and-recommitted protocol
    # must NOT become its own new freeze. The guard's "frozen commit" is
    # always the ORIGINAL commit that first added the path.
    _write_protocol(isolated_repo, "version one\n")
    original_commit = _commit_all(isolated_repo, "freeze v1")

    protocol_path = isolated_repo / "docs" / "research" / "FROZEN_APPEARANCE_CALIBRATION_POLICY_2026_27.md"
    protocol_path.write_text("version two -- edited\n", encoding="utf-8", newline="\n")
    _commit_all(isolated_repo, "edit protocol (violates Change Control)")

    result = wrapper.check_protocol_tracked_ancestry_and_blob(protocol_path=_PROTOCOL_REPO_PATH)

    assert result.protocol_commit == original_commit


def test_guard_rejects_a_committed_edit_to_the_same_protocol_path(isolated_repo):
    # The required behaviour change: a second COMMITTED edit at the same
    # path must FAIL this guard (not silently become a new "frozen commit"
    # as it incorrectly did before). Change Control requires a genuine new
    # protocol version to use a new versioned filename, never a second
    # commit at this path.
    _write_protocol(isolated_repo, "version one\n")
    _commit_all(isolated_repo, "freeze v1")

    protocol_path = isolated_repo / "docs" / "research" / "FROZEN_APPEARANCE_CALIBRATION_POLICY_2026_27.md"
    protocol_path.write_text("version two -- edited\n", encoding="utf-8", newline="\n")
    _commit_all(isolated_repo, "edit protocol (violates Change Control)")

    result = wrapper.check_protocol_tracked_ancestry_and_blob(protocol_path=_PROTOCOL_REPO_PATH)

    # HEAD's blob (the edited "version two") no longer matches the blob at
    # the ORIGINAL addition commit ("version one") -- so blob_matches_working_tree
    # is False and the guard as a whole does not pass.
    assert not result.blob_matches_working_tree
    assert not result.passed


def test_guard_accepts_an_unrelated_later_commit_that_never_touches_the_protocol(isolated_repo):
    _write_protocol(isolated_repo)
    protocol_commit = _commit_all(isolated_repo, "freeze protocol")
    (isolated_repo / "unrelated.txt").write_text("unrelated work\n", encoding="utf-8")
    _commit_all(isolated_repo, "unrelated later commit")

    result = wrapper.check_protocol_tracked_ancestry_and_blob(protocol_path=_PROTOCOL_REPO_PATH)

    assert result.protocol_commit == protocol_commit
    assert result.passed


# ---------------------------------------------------------------------------
# Guard Requirement 5: policy implementation BLOB identity (not ancestry).
# ---------------------------------------------------------------------------


def _write_policy_module(repo_dir: Path, text: str) -> Path:
    module_dir = repo_dir / "src" / "fpl_model" / "validation"
    module_dir.mkdir(parents=True, exist_ok=True)
    module_path = module_dir / "appearance_policy_backtest.py"
    module_path.write_text(text, encoding="utf-8")
    return module_path


def test_policy_implementation_guard_accepts_when_head_blob_matches_frozen_source_blob(isolated_repo):
    _write_policy_module(isolated_repo, "# v1\n")
    frozen_commit = _commit_all(isolated_repo, "policy impl v1")

    # A later, UNRELATED commit that never touches the module.
    (isolated_repo / "unrelated.txt").write_text("x", encoding="utf-8")
    _commit_all(isolated_repo, "unrelated later commit")

    current_commit = wrapper.check_policy_implementation_matches_frozen_source_blob(
        frozen_source_commit=frozen_commit
    )
    assert current_commit == frozen_commit


def test_policy_implementation_guard_rejects_a_descendant_commit_that_modifies_the_module(isolated_repo):
    # The required behaviour: a descendant commit that REWRITES the module
    # must fail, even though it is genuinely a descendant of the frozen
    # source commit -- ancestry alone does not prove content is unchanged.
    _write_policy_module(isolated_repo, "# v1\n")
    frozen_commit = _commit_all(isolated_repo, "policy impl v1")

    _write_policy_module(isolated_repo, "# v1\n# a later content-changing edit\n")
    _commit_all(isolated_repo, "policy impl v2 (changed content)")

    with pytest.raises(wrapper.GuardFailure, match="does not match its blob"):
        wrapper.check_policy_implementation_matches_frozen_source_blob(
            frozen_source_commit=frozen_commit
        )


def test_policy_implementation_guard_rejects_uncommitted_module_modifications(isolated_repo):
    module_path = _write_policy_module(isolated_repo, "# v1\n")
    frozen_commit = _commit_all(isolated_repo, "policy impl v1")

    module_path.write_text("# v1\n# uncommitted local edit\n", encoding="utf-8")

    with pytest.raises(wrapper.GuardFailure, match="uncommitted"):
        wrapper.check_policy_implementation_matches_frozen_source_blob(
            frozen_source_commit=frozen_commit
        )


# ---------------------------------------------------------------------------
# Full pipeline: GW1 materialization / GW6-15 filtering / global_vs_raw only /
# no early metric peeking / missing-intermediate-GW rejection.
# ---------------------------------------------------------------------------


def _freeze_protocol_and_policy_module(repo_dir: Path, *, committed_at: str) -> str:
    _write_protocol(repo_dir)
    _write_policy_module(repo_dir, "# frozen policy impl\n")
    return _commit_all(repo_dir, "freeze protocol and policy impl", committed_at=committed_at)


def _vaastav_shaped(gameweeks_frame):
    """Drop the synthetic fixture's ``code`` column to match real vaastav ``merged_gw.csv`` shape.

    The real ``merged_gw.csv`` has no ``code`` column -- ``run()``'s own
    ``.merge(players_raw[["id", "code"]], ...)`` step exists specifically to
    attach it. ``tests.test_appearance_policy_backtest._gameweeks`` includes
    ``code`` directly (other tests in that file pass it straight through
    without the merge), so a fake ``_archive_and_import`` must strip it here
    or the wrapper's merge would produce ``code_x``/``code_y`` instead of
    ``code``.
    """
    return gameweeks_frame.drop(columns=["code"])


def _freeze_and_patch(isolated_repo, monkeypatch, *, committed_at="2020-01-01T00:00:00+00:00"):
    frozen_commit = _freeze_protocol_and_policy_module(isolated_repo, committed_at=committed_at)
    monkeypatch.setattr(wrapper, "POLICY_IMPLEMENTATION_SOURCE_COMMIT", frozen_commit)


def _patch_archive(monkeypatch, *, result, players_frame, gameweeks_frame):
    def _fake_archive_and_import(*, season, revision_sha, raw_root, database_path):
        return result, players_frame, _vaastav_shaped(gameweeks_frame)

    monkeypatch.setattr(wrapper, "_archive_and_import", _fake_archive_and_import)


def test_run_materializes_from_gw1_but_only_scores_gw6_through_gw15(isolated_repo, tmp_path, monkeypatch):
    _freeze_and_patch(isolated_repo, monkeypatch)

    result, database_path, players_frame, gameweeks_frame = _import(
        tmp_path, num_gameweeks=16, vary_starts=True
    )
    _patch_archive(monkeypatch, result=result, players_frame=players_frame, gameweeks_frame=gameweeks_frame)

    run_result = wrapper.run(revision=None, raw_root=tmp_path, database_path=database_path)

    assert run_result["confirmatory_evaluation_window"] == [6, 15]
    assert run_result["burn_in_gameweeks"] == [1, 5]
    assert sorted(run_result["evaluated_gameweeks"]) == list(range(6, 16))
    # Only global_vs_raw is emitted as a STRUCTURED comparison -- no other
    # policy has its own metrics/comparison entry. (The prose $schema_note
    # legitimately mentions "high_end_shrinkage" to document that it is not
    # inspected/emitted/compared/used -- this checks the structured,
    # machine-read fields only, not the free-text note.)
    assert set(run_result["policy_metrics"]) == {"raw", "global"}
    assert run_result["comparison"]["focus_policy"] == "global"
    assert run_result["comparison"]["comparator_policy"] == "raw"
    assert "comparisons" not in run_result  # not the multi-comparison dict shape
    assert run_result["verdict"] in {"confirms", "does_not_replicate", "ambiguous"}
    assert run_result["checkpoint_reached"] is True
    assert run_result["required_confirmatory_gameweek_clusters"] == 10


def test_run_uses_frozen_bootstrap_constants_and_accepts_no_override(isolated_repo, tmp_path, monkeypatch):
    # Guard Requirement 8: run() must not accept alternative resamples/seed
    # values -- its signature has no such parameters at all.
    import inspect

    signature = inspect.signature(wrapper.run)
    assert "resamples" not in signature.parameters
    assert "seed" not in signature.parameters

    _freeze_and_patch(isolated_repo, monkeypatch)
    result, database_path, players_frame, gameweeks_frame = _import(
        tmp_path, num_gameweeks=16, vary_starts=True
    )
    _patch_archive(monkeypatch, result=result, players_frame=players_frame, gameweeks_frame=gameweeks_frame)

    run_result = wrapper.run(revision=None, raw_root=tmp_path, database_path=database_path)
    assert run_result["comparison"]["mae_improvement"]["resamples"] == wrapper.BOOTSTRAP_RESAMPLES
    assert run_result["comparison"]["mae_improvement"]["seed"] == wrapper.BOOTSTRAP_SEED
    assert wrapper.BOOTSTRAP_RESAMPLES == 10_000
    assert wrapper.BOOTSTRAP_SEED == 42


def test_run_rejects_when_an_intermediate_gw_is_missing_even_if_gw15_is_present(
    isolated_repo, tmp_path, monkeypatch
):
    # checkpoint_reached must not be satisfied merely by GW15 being present
    # -- every GW6..GW15 label must be present. This builds a 16-gameweek
    # archive (so GW15 exists) and then DROPS GW10 rows entirely to prove an
    # intermediate gap is caught, not just a missing tail.
    _freeze_and_patch(isolated_repo, monkeypatch)
    result, database_path, players_frame, gameweeks_frame = _import(
        tmp_path, num_gameweeks=16, vary_starts=True
    )
    gaps_only_gw10_removed = gameweeks_frame.loc[gameweeks_frame["GW"] != 10].reset_index(drop=True)
    _patch_archive(
        monkeypatch, result=result, players_frame=players_frame, gameweeks_frame=gaps_only_gw10_removed
    )

    with pytest.raises(wrapper.GuardFailure, match=r"missing gameweek\(s\).*10"):
        wrapper.run(revision=None, raw_root=tmp_path, database_path=database_path)


def test_run_refuses_to_compute_or_emit_any_metric_before_the_checkpoint_is_complete(
    isolated_repo, tmp_path, monkeypatch
):
    # No early-peek path exists at all: with only 10 archived gameweeks
    # (GW15 not yet reached), run() must raise BEFORE computing any paired
    # row, metric, or bootstrap result -- proven here by monkeypatching
    # cluster_bootstrap to explode if it is ever called.
    _freeze_and_patch(isolated_repo, monkeypatch)
    result, database_path, players_frame, gameweeks_frame = _import(
        tmp_path, num_gameweeks=10, vary_starts=True
    )
    _patch_archive(monkeypatch, result=result, players_frame=players_frame, gameweeks_frame=gameweeks_frame)

    def _explode(*args, **kwargs):
        raise AssertionError("cluster_bootstrap must not be called before the checkpoint is complete")

    monkeypatch.setattr(wrapper, "cluster_bootstrap", _explode)

    with pytest.raises(wrapper.GuardFailure, match="checkpoint is not complete"):
        wrapper.run(revision=None, raw_root=tmp_path, database_path=database_path)


def test_run_no_longer_accepts_an_early_report_override(isolated_repo, tmp_path, monkeypatch):
    # Guard Requirement 5 (removal): the early-report escape hatch must not
    # exist on run() at all -- passing it is a TypeError, not a code path.
    import inspect

    signature = inspect.signature(wrapper.run)
    assert "allow_ambiguous_early_report" not in signature.parameters


def test_check_confirmatory_data_readiness_reports_no_metrics_or_verdict(isolated_repo, tmp_path, monkeypatch):
    # The safe substitute for an early-progress check: only gameweek
    # presence/absence, never a metric, CI, or verdict field.
    result, database_path, players_frame, gameweeks_frame = _import(
        tmp_path, num_gameweeks=10, vary_starts=True
    )
    _patch_archive(monkeypatch, result=result, players_frame=players_frame, gameweeks_frame=gameweeks_frame)

    readiness = wrapper.check_confirmatory_data_readiness(
        revision=None, raw_root=tmp_path, database_path=database_path
    )

    assert readiness["present_gameweeks"] == [6, 7, 8, 9, 10]
    assert readiness["missing_gameweeks"] == [11, 12, 13, 14, 15]
    assert readiness["checkpoint_ready"] is False
    forbidden_keys = {
        "mae_improvement", "rmse_improvement", "policy_metrics", "verdict", "comparison",
    }
    assert forbidden_keys.isdisjoint(readiness.keys())


def test_run_rejects_a_protocol_committed_after_the_gw6_deadline(isolated_repo, tmp_path, monkeypatch):
    # Commit the protocol at a time that postdates GW6's deadline in the
    # synthetic fixture (kickoffs start 2025-08-16) -- this must be rejected
    # regardless of how much data is otherwise available.
    _freeze_and_patch(isolated_repo, monkeypatch, committed_at="2030-01-01T00:00:00+00:00")

    result, database_path, players_frame, gameweeks_frame = _import(
        tmp_path, num_gameweeks=16, vary_starts=True
    )
    _patch_archive(monkeypatch, result=result, players_frame=players_frame, gameweeks_frame=gameweeks_frame)

    with pytest.raises(wrapper.GuardFailure, match="does not predate"):
        wrapper.run(revision=None, raw_root=tmp_path, database_path=database_path)


# ---------------------------------------------------------------------------
# Contract test: the frozen protocol DOCUMENT must declare the same cluster
# constant the wrapper enforces in code. The wrapper's
# REQUIRED_CONFIRMATORY_GAMEWEEK_CLUSTERS=10 is a real verdict rule
# (Predefined Verdict: a confirming/non-replicating verdict requires paired
# scored observations from all ten GW6-GW15 clusters) -- if the protocol
# document does not itself preregister this constant and rule, the code
# would be applying a verdict rule that was never actually frozen in the
# committed document. This reads the REAL protocol file on disk (never the
# isolated_repo fixture) so it fails the moment the two drift apart again.
# ---------------------------------------------------------------------------


def test_frozen_protocol_document_declares_the_required_cluster_constant():
    # wrapper.PROTOCOL_PATH is deliberately relative (matches its own
    # REPO_ROOT-relative git usage) -- resolve it against the real repo
    # root here rather than relying on the test runner's cwd.
    repo_root = Path(__file__).resolve().parents[1]
    protocol_text = (repo_root / wrapper.PROTOCOL_PATH).read_text(encoding="utf-8")

    assert "required_confirmatory_gameweek_clusters = 10" in protocol_text, (
        "the frozen protocol document must declare "
        "'required_confirmatory_gameweek_clusters = 10' under Frozen Model and Analysis "
        "Constants -- this is the same constant the wrapper enforces as "
        "REQUIRED_CONFIRMATORY_GAMEWEEK_CLUSTERS, and the document is the preregistration "
        "record for that rule"
    )
    assert str(wrapper.REQUIRED_CONFIRMATORY_GAMEWEEK_CLUSTERS) == "10", (
        "the wrapper's own frozen constant has drifted from the value this test (and the "
        "protocol document) expects"
    )
    assert (
        "requires paired scored observations from all ten gameweek clusters" in protocol_text
    ), (
        "the frozen protocol document must state the all-ten-clusters verdict rule under "
        "Predefined Verdict, not just declare the bare constant"
    )
