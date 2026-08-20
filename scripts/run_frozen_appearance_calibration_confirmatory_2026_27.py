"""Confirmatory 2026-27 evaluation of the frozen `global` appearance calibration policy.

Runs the protocol committed at
``docs/research/FROZEN_APPEARANCE_CALIBRATION_POLICY_2026_27.md`` -- see that
document for the full frozen research question, constants, cohort
definition, and predefined verdict rule. This script does not restate the
protocol; it implements exactly what the protocol requires and refuses to
run when a guard fails -- there is no partial/preview/early-report mode:
either every guard passes and the GW6-GW15 checkpoint is fully archived, in
which case ``run()`` returns a genuine confirmatory result, or it raises
``GuardFailure`` and returns/writes nothing. ``check_confirmatory_data_readiness``
is the only supported way to check progress before the checkpoint, and it
reports data availability only -- never a metric, CI, or verdict.

WHY A WRAPPER, NOT JUST NEW CLI FLAGS ON THE EXISTING SCRIPT: the existing
``scripts/backtest_appearance_calibration_policies.py`` (and the
``materialize_appearance_policy_backtest`` it calls) uses a single
``evaluation_from_gw``/``evaluation_to_gw`` pair to control BOTH (a) when
calibration rows begin accumulating in ``fit_for_gameweek``'s history and
(b) which rows are scored/reported. The frozen protocol requires these to
differ: calibration history must begin at GW1 (so the GW1-GW5 burn-in period
can enter the causal fit), but only GW6-GW15 rows may contribute to the
confirmatory MAE/RMSE comparison. Calling the existing script's ``run()``
with ``--evaluation-from-gw 6`` would incorrectly discard the GW1-GW5
training history the protocol requires. This script instead calls
``materialize_appearance_policy_backtest`` directly with
``evaluation_from_gw=1, evaluation_to_gw=15``, then filters the SCORED
observations (not the calibration history) down to GW6-GW15 before pairing
and bootstrapping.

This script emits ONLY the ``global_vs_raw`` comparison -- per the protocol,
``high_end_shrinkage`` is excluded entirely from this confirmatory analysis
and must not appear in the output.

Every one of the seven Guard Requirements in the protocol's own "Guard
Requirements" section is checked before any confirmatory verdict is
computed; a failed guard raises (or, for the GW15-checkpoint guard,
degrades the result to an explicit non-verdict) rather than silently
producing a number.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.appearance_policy_backtest import (
    DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS,
    materialize_appearance_policy_backtest,
)
from fpl_model.validation.backtest import score_predictions
from fpl_model.validation.paired_uncertainty import (
    DEFAULT_RESAMPLES,
    DEFAULT_SEED,
    build_paired_rows,
    cluster_bootstrap,
)

# scripts/ has no __init__.py and is not an installed package (same
# bootstrap every other script-to-script reuse in this codebase needs) --
# the shim must run BEFORE this import, since module-level imports execute
# at import time, not inside main().
sys.path.insert(0, str(Path(__file__).resolve().parent))
import backtest_appearance_calibration_policies as policy_backtest_script  # noqa: E402

PROTOCOL_PATH = Path("docs/research/FROZEN_APPEARANCE_CALIBRATION_POLICY_2026_27.md")
PROTOCOL_VERSION = "appearance_global_calibration_confirmatory_2026_27_v1"
# Full 40-character SHA (not an abbreviation) -- Guard Requirement 5 needs an
# exact, unambiguous commit to fetch the frozen blob from.
POLICY_IMPLEMENTATION_SOURCE_COMMIT = "2c2c57d8acaa0b6193fa51e5232bd919c8ae640e"

SEASON = "2026-27"
BURN_IN_FROM_GW = 1
BURN_IN_TO_GW = 5
CONFIRMATORY_FROM_GW = 6
CONFIRMATORY_TO_GW = 15
MATERIALIZE_TO_GW = CONFIRMATORY_TO_GW  # GW1..GW15 materialized; GW6..GW15 scored.

# Frozen constants -- "Frozen Model and Analysis Constants". These are not
# CLI-configurable: changing any of them creates a new protocol version, not
# a flag on this one.
MINIMUM_CALIBRATION_GAMEWEEKS = 5
DEADLINE_BUFFER = timedelta(minutes=90)
OUTCOME_DELAY = timedelta(hours=3)
SHORT_FORM_GAMEWEEKS = 6
DEFCON_SHORT_FORM_GAMEWEEKS = 10
LONG_FORM_WEIGHT = 0.8
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 42
# The protocol's "insufficient eligible evaluation clusters" language had no
# numeric definition; since the confirmatory window is the fixed GW6-GW15
# (exactly ten gameweek clusters), a confirming/non-replicating verdict
# requires paired scored rows from ALL TEN clusters -- this is frozen NOW,
# before the protocol document is committed, precisely to avoid that
# ambiguity being resolved only after seeing results.
REQUIRED_CONFIRMATORY_GAMEWEEK_CLUSTERS = 10


def _validate_frozen_configuration() -> None:
    """Explicit (non-``assert``) validation of the frozen constants above.

    ``assert`` statements are stripped under Python's ``-O``/optimized mode,
    which would silently disable this check in exactly the deployment
    configurations where a mismatch matters most -- an explicit
    ``ValueError`` is not optimized away.
    """
    if MINIMUM_CALIBRATION_GAMEWEEKS != DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS:
        raise ValueError(
            "MINIMUM_CALIBRATION_GAMEWEEKS no longer matches "
            "appearance_policy_backtest.DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS -- the frozen "
            "constant has drifted from the module default"
        )
    if BOOTSTRAP_RESAMPLES != DEFAULT_RESAMPLES:
        raise ValueError(
            "BOOTSTRAP_RESAMPLES no longer matches paired_uncertainty.DEFAULT_RESAMPLES"
        )
    if BOOTSTRAP_SEED != DEFAULT_SEED:
        raise ValueError("BOOTSTRAP_SEED no longer matches paired_uncertainty.DEFAULT_SEED")
    if CONFIRMATORY_TO_GW - CONFIRMATORY_FROM_GW + 1 != REQUIRED_CONFIRMATORY_GAMEWEEK_CLUSTERS:
        raise ValueError(
            "REQUIRED_CONFIRMATORY_GAMEWEEK_CLUSTERS no longer matches the GW6-GW15 window width"
        )


_validate_frozen_configuration()


class GuardFailure(ValueError):
    """Raised when a Guard Requirement from the frozen protocol is violated."""


# Module-level so tests can point the guard at an isolated throwaway git
# repository fixture instead of this real repository -- every guard
# function below reads this indirectly via _run_git/_git rather than taking
# its own cwd parameter, so the guard functions' signatures stay exactly
# what the protocol describes.
REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], capture_output=True, text=True, cwd=REPO_ROOT)
    if check and result.returncode != 0:
        raise GuardFailure(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _git(*args: str) -> str:
    return _run_git(*args).stdout.strip()


@dataclass(frozen=True, slots=True)
class ProtocolGuardResult:
    """Outcome of Guard Requirements 1-3: tracked/clean, ancestry, blob match."""

    protocol_commit: str
    protocol_committed_at: datetime
    run_commit: str
    tracked_and_clean: bool
    is_ancestor_of_run_commit: bool
    blob_matches_working_tree: bool

    @property
    def passed(self) -> bool:
        return (
            self.tracked_and_clean
            and self.is_ancestor_of_run_commit
            and self.blob_matches_working_tree
        )


def _original_addition_commit(repo_relative: str) -> str:
    """The commit that FIRST added ``repo_relative`` -- the immutable freeze commit.

    Uses ``git log --diff-filter=A --follow`` to find the earliest commit
    that introduced this exact path, not the most recent commit that
    touched it. This is deliberate: treating the latest touching commit as
    "the frozen commit" would let an edited-and-recommitted protocol
    silently become its own new freeze and pass every later check. Change
    Control requires a genuine new protocol version to use a NEW versioned
    filename; a second commit at the SAME path is therefore always a
    violation, and must be caught by comparing against the ORIGINAL
    addition, not whatever happens to be most recent.

    ``--follow`` is included for robustness against a path rename, but this
    protocol's own Change Control forbids renaming a frozen document, so in
    practice the plain path history is what matters.
    """
    log_output = _git(
        "log", "--diff-filter=A", "--follow", "--format=%H", "--reverse", "--", repo_relative
    )
    if not log_output:
        raise GuardFailure(f"{repo_relative} has no commit that added it -- it must be committed")
    # --reverse with --format=%H yields oldest-first; the FIRST line is the
    # original addition commit.
    return log_output.splitlines()[0]


def check_protocol_tracked_ancestry_and_blob(*, protocol_path: Path) -> ProtocolGuardResult:
    """Guard Requirements 1-3.

    1. the document is tracked and clean (no uncommitted working-tree diff);
    2. its ORIGINAL freeze commit (the commit that first ADDED this exact
       path, per ``_original_addition_commit`` -- never a later commit that
       merely touched the same path) is an ancestor of the run commit (HEAD);
    3. the blob at that ORIGINAL freeze commit matches BOTH the blob at HEAD
       and the clean working tree for the same path. A later commit that
       edits and re-commits the same path therefore FAILS this check (its
       HEAD blob differs from the original-addition blob), exactly as
       Change Control requires: a genuine new protocol version must use a
       new versioned filename, not a second commit at this path.
    """
    repo_relative = protocol_path.as_posix()

    # `git ls-files --error-unmatch` exits non-zero for an untracked path --
    # that is an expected outcome here (tracked=False), not a tool failure,
    # so it must not raise via _run_git's default check=True.
    tracked = _run_git("ls-files", "--error-unmatch", repo_relative, check=False).returncode == 0
    if not tracked:
        raise GuardFailure(
            f"{repo_relative} is not tracked by git -- it must be committed before a "
            "confirmatory run"
        )

    diff = _git("diff", "--", repo_relative)
    diff_cached = _git("diff", "--cached", "--", repo_relative)
    clean = not diff and not diff_cached
    tracked_and_clean = tracked and clean

    protocol_commit = _original_addition_commit(repo_relative)
    committed_at_raw = _git("log", "-1", "--format=%cI", protocol_commit)
    protocol_committed_at = datetime.fromisoformat(committed_at_raw)

    run_commit = _git("rev-parse", "HEAD")

    is_ancestor = _run_git(
        "merge-base", "--is-ancestor", protocol_commit, run_commit, check=False
    ).returncode == 0

    # Compare the blob stored AT THE ORIGINAL FREEZE COMMIT against BOTH the
    # blob at HEAD and the clean working tree for the same path -- all via
    # `git show`/git's own tracked state (never a raw file read), so CRLF/LF
    # autocrlf differences can't produce a false mismatch (the same lesson
    # from the earlier byte-identity false alarm elsewhere in this
    # codebase). "tracked_and_clean" above already guarantees the working
    # tree equals HEAD's blob for this path when true, so this is the check
    # that actually catches a later edit-and-recommit at the same path: if
    # anyone committed a change after the original addition, HEAD's blob
    # (and the working tree) will differ from the ORIGINAL commit's blob,
    # and this correctly evaluates to False.
    original_blob = _git("show", f"{protocol_commit}:{repo_relative}")
    head_blob = _git("show", f"HEAD:{repo_relative}")
    blob_matches = tracked_and_clean and original_blob == head_blob

    return ProtocolGuardResult(
        protocol_commit=protocol_commit,
        protocol_committed_at=protocol_committed_at,
        run_commit=run_commit,
        tracked_and_clean=tracked_and_clean,
        is_ancestor_of_run_commit=is_ancestor,
        blob_matches_working_tree=blob_matches,
    )


def confirmatory_deadline(*, gameweeks_frame: pd.DataFrame) -> datetime:
    """The first confirmatory deadline: GW6's target_deadline.

    Mirrors the protocol's own definition (``earliest gameweek kickoff -
    90 minutes``), applied to GW6 specifically -- Guard Requirement 4 checks
    the protocol's freeze commit predates THIS timestamp, not GW1's.
    """
    # The raw vaastav gameweeks frame's gameweek column is "GW" (matching
    # materialize_appearance_policy_backtest's own
    # ``gameweeks_frame["GW"] == gameweek`` lookup), not "gameweek".
    gw6_kickoffs = gameweeks_frame.loc[
        gameweeks_frame["GW"] == CONFIRMATORY_FROM_GW, "kickoff_time"
    ]
    if gw6_kickoffs.empty:
        raise GuardFailure("no GW6 fixtures found in gameweeks_frame -- cannot compute deadline")
    earliest_kickoff = pd.to_datetime(gw6_kickoffs, utc=True).min().to_pydatetime()
    return earliest_kickoff - DEADLINE_BUFFER


def check_protocol_predates_confirmatory_deadline(
    *, protocol_committed_at: datetime, deadline: datetime
) -> bool:
    """Guard Requirement 4 (corrected direction, strict inequality).

    The guard must reject a protocol commit that does NOT predate the first
    confirmatory deadline (GW6's deadline) -- i.e. committed too late. It
    must NOT reject a protocol merely because it predates the archived
    season data; a protocol correctly predating the archive is the
    intended, correct condition, not a failure.

    The protocol says the freeze commit must PREDATE the deadline -- a
    commit timestamped EXACTLY at the deadline has not predated it, so the
    comparison is a strict ``<``, not ``<=``.

    Both timestamps are tz-aware (git's ``%cI`` format is a strict ISO 8601
    offset; ``deadline`` derives from ``kickoff_time`` parsed with
    ``utc=True`` -- see ``materialize_appearance_policy_backtest``), so a
    direct comparison after normalizing to UTC is safe without any
    naive/aware branching.
    """
    return protocol_committed_at.astimezone(deadline.tzinfo) < deadline


def check_policy_implementation_matches_frozen_source_blob(*, frozen_source_commit: str) -> str:
    """Guard Requirement 5: implementation BLOB identity, not commit ancestry.

    Ancestry alone ("the current commit is a descendant of the frozen
    source commit") does NOT prove the policy implementation is unchanged
    -- a descendant commit may have rewritten the module completely while
    still being a legitimate descendant. This checks the actual file
    CONTENT instead: the current HEAD blob of
    ``src/fpl_model/validation/appearance_policy_backtest.py`` must be
    byte-identical to that file's blob AT the frozen source commit, and the
    module must carry no staged or unstaged working-tree modifications.

    A later commit that modifies the module -- even one that is a
    descendant of ``frozen_source_commit`` -- fails this check, because its
    HEAD blob differs from the frozen blob. An unrelated later repository
    commit (one that never touches this module) passes, because the
    module's blob at HEAD is unchanged.

    Any future FIELD-EQUIVALENT reimplementation of the same policy still
    requires a new, explicitly reviewed protocol/version -- ancestry, or
    any other automatic heuristic, must never silently license it. This
    function only proves byte-identity with the ORIGINAL frozen blob.
    """
    module_path = "src/fpl_model/validation/appearance_policy_backtest.py"

    diff = _git("diff", "--", module_path)
    diff_cached = _git("diff", "--cached", "--", module_path)
    if diff or diff_cached:
        raise GuardFailure(
            f"{module_path} has uncommitted (staged or unstaged) modifications -- the policy "
            "implementation must be in a clean, committed state before a confirmatory run"
        )

    frozen_blob = _run_git(
        "show", f"{frozen_source_commit}:{module_path}", check=False
    )
    if frozen_blob.returncode != 0:
        raise GuardFailure(
            f"{module_path} does not exist at frozen source commit {frozen_source_commit}: "
            f"{frozen_blob.stderr.strip()}"
        )
    head_blob = _run_git("show", f"HEAD:{module_path}", check=False)
    if head_blob.returncode != 0:
        raise GuardFailure(f"{module_path} does not exist at HEAD: {head_blob.stderr.strip()}")

    current_commit = _git("log", "-1", "--format=%H", "--", module_path)
    if frozen_blob.stdout != head_blob.stdout:
        raise GuardFailure(
            f"{module_path}'s blob at HEAD does not match its blob at the frozen source commit "
            f"{frozen_source_commit} -- the policy implementation has changed since the freeze "
            "(this is a BLOB comparison, not commit ancestry: a later commit, even one that is "
            "a descendant of the frozen source commit, that modifies this module's content "
            "fails this check). A field-equivalent reimplementation requires a new, explicitly "
            "reviewed protocol/version; this guard does not and must not license one "
            "automatically."
        )
    return current_commit


def _archive_and_import(*, season: str, revision_sha: str | None, raw_root: Path, database_path: Path):
    return policy_backtest_script._archive_and_import(
        season=season, revision_sha=revision_sha, raw_root=raw_root, database_path=database_path
    )


def run(
    *,
    revision: str | None,
    raw_root: Path,
    database_path: Path,
    protocol_path: Path = PROTOCOL_PATH,
) -> dict[str, object]:
    """Run the confirmatory protocol end to end. Bootstrap resamples/seed are NOT configurable here.

    The frozen protocol fixes ``bootstrap_resamples = 10,000`` and
    ``bootstrap_seed = 42`` as immutable constants -- this public entry
    point deliberately accepts no ``resamples``/``seed`` override so a
    caller cannot produce an apparently-confirmatory result with
    non-frozen bootstrap parameters. Tests that need a different seam
    should call the private helpers directly or monkeypatch
    ``cluster_bootstrap`` itself, never pass alternate values through here.
    """
    # --- Guard Requirements 1-3: protocol document integrity/ancestry. ---
    protocol_guard = check_protocol_tracked_ancestry_and_blob(protocol_path=protocol_path)
    if not protocol_guard.tracked_and_clean:
        raise GuardFailure(
            f"{protocol_path} is not tracked and clean -- it must be committed with no "
            "uncommitted working-tree changes before a confirmatory run"
        )
    if not protocol_guard.is_ancestor_of_run_commit:
        raise GuardFailure(
            f"protocol commit {protocol_guard.protocol_commit} is not an ancestor of the run "
            f"commit {protocol_guard.run_commit}"
        )
    if not protocol_guard.blob_matches_working_tree:
        raise GuardFailure(
            f"{protocol_path}'s working-tree contents do not match the blob stored at its "
            f"frozen commit {protocol_guard.protocol_commit} -- the document may have been "
            "edited after freezing, which Change Control forbids"
        )

    imported, players_raw, gameweeks = _archive_and_import(
        season=SEASON, revision_sha=revision, raw_root=raw_root, database_path=database_path
    )
    gameweeks_with_code = gameweeks.merge(
        players_raw.loc[:, ["id", "code"]], left_on="element", right_on="id", how="left"
    )

    # --- Guard Requirement 4 (corrected direction): freeze predates GW6 deadline. ---
    deadline = confirmatory_deadline(gameweeks_frame=gameweeks_with_code)
    protocol_is_timely = check_protocol_predates_confirmatory_deadline(
        protocol_committed_at=protocol_guard.protocol_committed_at, deadline=deadline
    )
    if not protocol_is_timely:
        raise GuardFailure(
            f"protocol commit {protocol_guard.protocol_commit} "
            f"({protocol_guard.protocol_committed_at.isoformat()}) does not predate the first "
            f"confirmatory deadline ({deadline.isoformat()}) -- a protocol committed after its "
            "own confirmatory deadline cannot license a confirmatory verdict. (Predating the "
            "archived season data, by contrast, is the intended and correct condition, not a "
            "failure.)"
        )

    # --- Guard Requirement 5: policy implementation BLOB matches the frozen source. ---
    policy_implementation_commit = check_policy_implementation_matches_frozen_source_blob(
        frozen_source_commit=POLICY_IMPLEMENTATION_SOURCE_COMMIT
    )

    # --- Guard Requirement 6: materialize from GW1, but only GW6-15 may score. ---
    with duckdb.connect(str(database_path)) as connection:
        bundle = materialize_appearance_policy_backtest(
            season=SEASON,
            import_run_id=imported.import_run_id,
            connection=connection,
            gameweeks_frame=gameweeks_with_code,
            players_raw_frame=players_raw,
            evaluation_from_gw=BURN_IN_FROM_GW,
            evaluation_to_gw=MATERIALIZE_TO_GW,
            deadline_buffer=DEADLINE_BUFFER,
            outcome_delay=OUTCOME_DELAY,
            short_form_gameweeks=SHORT_FORM_GAMEWEEKS,
            defcon_short_form_gameweeks=DEFCON_SHORT_FORM_GAMEWEEKS,
            long_form_weight=LONG_FORM_WEIGHT,
            minimum_calibration_gameweeks=MINIMUM_CALIBRATION_GAMEWEEKS,
        )

    for policy in ("raw", "global"):
        if not bundle.results_by_policy[policy].observations:
            raise ValueError(f"policy={policy!r} produced no scored observations")

    confirmatory_gameweeks = tuple(
        gw for gw in bundle.evaluated_gameweeks if CONFIRMATORY_FROM_GW <= gw <= CONFIRMATORY_TO_GW
    )

    # --- Checkpoint completeness gate: BEFORE any pairing, metric, or
    # bootstrap computation. A non-verdict label does not make an interim
    # MAE/CI preview harmless -- the NUMBERS themselves are the peek, so
    # nothing downstream of this point may run until every required
    # gameweek label is present. `CONFIRMATORY_TO_GW in confirmatory_gameweeks`
    # alone is insufficient (it would pass with GW6-14 missing and only
    # GW15 present, or with a partial archive) -- every label GW6..GW15
    # must be present.
    required_gameweeks = set(range(CONFIRMATORY_FROM_GW, CONFIRMATORY_TO_GW + 1))
    missing_gameweeks = sorted(required_gameweeks - set(confirmatory_gameweeks))
    if missing_gameweeks:
        raise GuardFailure(
            f"GW{CONFIRMATORY_FROM_GW}-GW{CONFIRMATORY_TO_GW} checkpoint is not complete -- "
            f"missing gameweek(s): {missing_gameweeks} (evaluated so far: "
            f"{sorted(confirmatory_gameweeks)}). This script must not compute or emit any "
            "metric, paired row, or bootstrap result before every required gameweek label is "
            "present in the archive -- re-run once the archive includes GW"
            f"{CONFIRMATORY_FROM_GW}-GW{CONFIRMATORY_TO_GW} in full. NOTE: this only checks "
            "that each GAMEWEEK LABEL has at least one evaluated row; it does NOT independently "
            "verify a partially-archived gameweek contains EVERY scheduled fixture for that "
            "gameweek -- that would require checking against an authoritative expected-fixture "
            "set, which this script does not have access to and does not claim to check."
        )

    def _confirmatory_only(observations):
        return tuple(
            observation
            for observation in observations
            if CONFIRMATORY_FROM_GW <= observation.gameweek <= CONFIRMATORY_TO_GW
        )

    raw_confirmatory = _confirmatory_only(bundle.results_by_policy["raw"].observations)
    global_confirmatory = _confirmatory_only(bundle.results_by_policy["global"].observations)

    raw_key_set = {(o.player_id, o.fixture_id, o.gameweek) for o in raw_confirmatory}
    global_key_set = {(o.player_id, o.fixture_id, o.gameweek) for o in global_confirmatory}
    if raw_key_set != global_key_set:
        raise ValueError(
            "global and raw policies scored different GW6-15 candidate row sets -- per "
            "Evaluation Cohort, any row-set mismatch invalidates the run"
        )

    confirmatory_gaps = tuple(
        gap for gap in bundle.gaps if CONFIRMATORY_FROM_GW <= gap.gameweek <= CONFIRMATORY_TO_GW
    )

    # --- Guard Requirement 7: only global_vs_raw is emitted. high_end_shrinkage
    # IS still internally constructed by the shared materializer above (it
    # scores all three policies from one shared walk-forward pass, by
    # design -- see materialize_appearance_policy_backtest), but from this
    # point on it is not inspected, emitted, compared, or used in the
    # confirmatory verdict in any way. ---
    paired_rows = build_paired_rows(global_confirmatory, raw_confirmatory)
    # Belt-and-suspenders: build_paired_rows joins by key, but the source
    # sequences were already filtered to GW6-15 above, so every paired row's
    # own .gameweek must independently fall in that window too.
    if not all(CONFIRMATORY_FROM_GW <= row.gameweek <= CONFIRMATORY_TO_GW for row in paired_rows):
        raise ValueError("a paired row fell outside the GW6-15 confirmatory window -- internal bug")

    # --- Frozen bootstrap constants: always 10,000 resamples, seed 42 --
    # never caller-configurable (see run()'s own docstring). ---
    mae_result, rmse_result = cluster_bootstrap(
        paired_rows,
        cluster_key=lambda row: row.gameweek,
        cluster_label="gameweek",
        resamples=BOOTSTRAP_RESAMPLES,
        seed=BOOTSTRAP_SEED,
    )

    mae_dict = policy_backtest_script._bootstrap_to_dict(mae_result)
    rmse_dict = policy_backtest_script._bootstrap_to_dict(rmse_result)

    distinct_clusters = len({row.gameweek for row in paired_rows})
    missing_clusters = sorted(required_gameweeks - {row.gameweek for row in paired_rows})
    # Frozen Requirement (see "Frozen Model and Analysis Constants" /
    # required_confirmatory_gameweek_clusters): a confirming/non-replicating
    # verdict requires paired scored rows from ALL TEN GW6-15 clusters, not
    # merely >= 2. If the archive checkpoint is complete (checked above) but
    # fewer than REQUIRED_CONFIRMATORY_GAMEWEEK_CLUSTERS clusters have any
    # paired row (e.g. every row in some gameweek was a gap on one side),
    # the verdict must be ambiguous, with the missing clusters reported.
    insufficient_clusters = distinct_clusters < REQUIRED_CONFIRMATORY_GAMEWEEK_CLUSTERS

    mae_ci_low, mae_ci_high = mae_dict["ci_low"], mae_dict["ci_high"]
    rmse_ci_low, rmse_ci_high = rmse_dict["ci_low"], rmse_dict["ci_high"]

    mae_positive = mae_ci_low > 0.0
    rmse_positive = rmse_ci_low > 0.0
    mae_negative = mae_ci_high < 0.0
    rmse_negative = rmse_ci_high < 0.0

    if insufficient_clusters:
        verdict = "ambiguous"
        verdict_reason = (
            f"only {distinct_clusters}/{REQUIRED_CONFIRMATORY_GAMEWEEK_CLUSTERS} required "
            f"evaluation clusters (gameweeks) have any paired row -- missing clusters: "
            f"{missing_clusters}"
        )
    elif mae_positive and rmse_positive:
        verdict = "confirms"
        verdict_reason = "both MAE and RMSE improvement CIs lie entirely above zero"
    elif mae_negative and rmse_negative:
        verdict = "does_not_replicate"
        verdict_reason = "both MAE and RMSE improvement CIs lie entirely below zero"
    else:
        verdict = "ambiguous"
        verdict_reason = (
            "at least one CI crosses zero, or MAE and RMSE disagree in direction"
        )

    raw_metrics = policy_backtest_script._metrics_to_dict(score_predictions(raw_confirmatory))
    global_metrics = policy_backtest_script._metrics_to_dict(score_predictions(global_confirmatory))

    return {
        "$schema_note": (
            "Confirmatory (not exploratory) 2026-27 evaluation of the frozen `global` "
            "appearance-calibration policy versus `raw`, run per "
            f"{protocol_path.as_posix()} (protocol_version={PROTOCOL_VERSION}). Calibration "
            f"history is materialized from GW{BURN_IN_FROM_GW} (burn-in through "
            f"GW{BURN_IN_TO_GW}), but only GW{CONFIRMATORY_FROM_GW}-GW{CONFIRMATORY_TO_GW} "
            "observations contribute to the reported metrics/verdict. The shared materializer "
            "internally constructs all three policies (raw/global/high_end_shrinkage) in one "
            "walk-forward pass, but high_end_shrinkage is not inspected, emitted, compared, or "
            "used in this confirmatory verdict in any way -- only global_vs_raw appears in this "
            "artifact. See the protocol document for the full frozen specification; this "
            "artifact does not restate it."
        ),
        "protocol_version": PROTOCOL_VERSION,
        "protocol_path": protocol_path.as_posix(),
        "protocol_commit": protocol_guard.protocol_commit,
        "protocol_committed_at": protocol_guard.protocol_committed_at.isoformat(),
        "run_commit": protocol_guard.run_commit,
        "policy_implementation_source_commit": POLICY_IMPLEMENTATION_SOURCE_COMMIT,
        "policy_implementation_current_commit": policy_implementation_commit,
        "season": SEASON,
        "import_run_id": imported.import_run_id,
        "archived_source_revision": imported.source_revision,
        "burn_in_gameweeks": [BURN_IN_FROM_GW, BURN_IN_TO_GW],
        "confirmatory_evaluation_window": [CONFIRMATORY_FROM_GW, CONFIRMATORY_TO_GW],
        "evaluated_gameweeks": list(confirmatory_gameweeks),
        # Always true by construction at this point: the checkpoint-completeness
        # gate above raises GuardFailure (before any metric/paired-row/bootstrap
        # computation) whenever GW6-GW15 is not fully present, so execution can
        # only reach this return with the checkpoint complete.
        "checkpoint_reached": True,
        "required_confirmatory_gameweek_clusters": REQUIRED_CONFIRMATORY_GAMEWEEK_CLUSTERS,
        "evaluated_row_count": len(paired_rows),
        "gap_count": len(confirmatory_gaps),
        "minimum_calibration_gameweeks": MINIMUM_CALIBRATION_GAMEWEEKS,
        "policy_metrics": {"raw": raw_metrics, "global": global_metrics},
        "comparison": {
            "focus_policy": "global",
            "comparator_policy": "raw",
            "paired_rows": len(paired_rows),
            "distinct_evaluation_clusters": distinct_clusters,
            "missing_evaluation_clusters": missing_clusters,
            "mae_improvement": mae_dict,
            "rmse_improvement": rmse_dict,
        },
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "guards": {
            "protocol_tracked_and_clean": protocol_guard.tracked_and_clean,
            "protocol_commit_is_ancestor_of_run_commit": protocol_guard.is_ancestor_of_run_commit,
            "protocol_blob_matches_frozen_commit": protocol_guard.blob_matches_working_tree,
            "protocol_predates_confirmatory_deadline": protocol_is_timely,
            "confirmatory_deadline": deadline.isoformat(),
            "policy_implementation_matches_frozen_source_blob": True,
            "materialized_from_gw1_scored_gw6_15_only": True,
            "only_global_vs_raw_emitted": True,
        },
        "reproduce": (
            ".venv\\Scripts\\python.exe "
            "scripts/run_frozen_appearance_calibration_confirmatory_2026_27.py"
        ),
    }


def check_confirmatory_data_readiness(
    *, revision: str | None, raw_root: Path, database_path: Path
) -> dict[str, object]:
    """Report GW6-GW15 data AVAILABILITY only -- never any metric, CI, or paired row.

    Removing all early-peek codepaths from ``run()`` (see its own docstring
    and the checkpoint-completeness gate inside it) means there is no
    supported way to preview the confirmatory comparison before GW15 is
    fully archived. This is the safe substitute for a "how close are we"
    check: it reports which of GW6-GW15 are present in the archive and
    which are missing, and NOTHING else -- no MAE, no CI, no policy
    metrics, no bootstrap result, no verdict. A non-verdict label does not
    make an interim numeric preview harmless; the NUMBERS themselves would
    constitute peeking, so this function computes none of them.
    """
    imported, players_raw, gameweeks = _archive_and_import(
        season=SEASON, revision_sha=revision, raw_root=raw_root, database_path=database_path
    )
    gameweeks_with_code = gameweeks.merge(
        players_raw.loc[:, ["id", "code"]], left_on="element", right_on="id", how="left"
    )
    present_gameweeks = sorted(
        int(gw)
        for gw in gameweeks_with_code.loc[
            gameweeks_with_code["GW"].between(CONFIRMATORY_FROM_GW, CONFIRMATORY_TO_GW), "GW"
        ].unique()
    )
    required_gameweeks = set(range(CONFIRMATORY_FROM_GW, CONFIRMATORY_TO_GW + 1))
    missing_gameweeks = sorted(required_gameweeks - set(present_gameweeks))
    return {
        "import_run_id": imported.import_run_id,
        "archived_source_revision": imported.source_revision,
        "confirmatory_evaluation_window": [CONFIRMATORY_FROM_GW, CONFIRMATORY_TO_GW],
        "present_gameweeks": present_gameweeks,
        "missing_gameweeks": missing_gameweeks,
        "checkpoint_ready": not missing_gameweeks,
        "note": (
            "Data availability only -- a GAMEWEEK LABEL appearing here means at least one row "
            "with that label exists in the raw archive; it does NOT independently verify the "
            "gameweek contains every scheduled fixture (that would require checking against an "
            "authoritative expected-fixture set, which this check does not have access to). "
            "This reports no metric, CI, paired row, or verdict of any kind."
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", help="Exact Git revision of 2026-27 merged_gw.csv to pin")
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw/vaastav"))
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "docs/research/appearance_calibration_confirmatory_2026_27.json"
        ),
    )
    parser.add_argument(
        "--check-readiness-only",
        action="store_true",
        help=(
            "Report GW6-GW15 data availability only (present/missing gameweek labels) and "
            "exit -- computes and prints no metric, CI, paired row, or verdict of any kind. "
            "Safe to run before the GW15 checkpoint; the full confirmatory run() is not."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.check_readiness_only:
        readiness = check_confirmatory_data_readiness(
            revision=args.revision, raw_root=args.raw_root, database_path=args.database
        )
        print(f"present_gameweeks={readiness['present_gameweeks']}")
        print(f"missing_gameweeks={readiness['missing_gameweeks']}")
        print(f"checkpoint_ready={readiness['checkpoint_ready']}")
        return

    result = run(revision=args.revision, raw_root=args.raw_root, database_path=args.database)
    output = json.dumps(result, indent=2, sort_keys=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output + "\n", encoding="utf-8")

    print(f"protocol_commit={result['protocol_commit']} run_commit={result['run_commit']}")
    print(
        f"evaluated_gameweeks={result['evaluated_gameweeks']} "
        f"evaluated_row_count={result['evaluated_row_count']} gap_count={result['gap_count']}"
    )
    comparison = result["comparison"]
    mae = comparison["mae_improvement"]
    rmse = comparison["rmse_improvement"]
    print(
        f"global_vs_raw: mae_improvement={mae['point_estimate']:+.4f} "
        f"ci=[{mae['ci_low']:.4f}, {mae['ci_high']:.4f}] "
        f"rmse_improvement={rmse['point_estimate']:+.4f} "
        f"ci=[{rmse['ci_low']:.4f}, {rmse['ci_high']:.4f}]"
    )
    print(f"verdict={result['verdict']} ({result['verdict_reason']})")
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
