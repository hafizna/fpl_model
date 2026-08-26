"""Run every release-level Sprint 5 gate against one named set of model runs.

Three independent checks now exist for one release (an anchor GW plus its
GW+1/GW+2 horizon, or any set of ``model_run_id``\\ s a caller names explicitly):

- ``validation.release_manifest.build_release_manifest`` -- lineage coherence;
- ``validation.release_freshness.check_release_freshness`` -- staleness, fixture
  completion, FPL finality, drift eligibility;
- ``validation.release_approval.check_release_approval`` -- APPROVED (not merely
  shadow) calibration/uncertainty lineage.

Each already runs and reports independently; this module is deliberately thin --
it does not re-implement any check, only calls all three against the same
``model_run_ids`` and folds their verdicts into one combined pass/fail. It is
VALIDATE-ONLY: it materialises nothing and writes nothing to the database. A
caller is responsible for having already produced the model runs it names here
(via the existing ``project_*``/``refresh_*`` pipeline commands) -- see
`docs/PIPELINE_ARCHITECTURE.md`. A pipeline that also materialises those runs
automatically is a separate, not-yet-built Sprint 5 item.

The 100%-shortlist ``decision_coverage`` gate is deliberately NOT included here.
Unlike the three release-level checks above, it does not take ``model_run_ids``
alone -- it evaluates one specific decision command's own owned-squad/shortlist
pools (lineup, transfer, rolling plan, or initial squad), which do not exist
until that command actually runs. It remains attached to each decision command's
own output instead (see `docs/PROJECTION_COVERAGE_AUDIT.md`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.validation.release_approval import check_release_approval
from fpl_model.validation.release_freshness import (
    DEFAULT_STALE_AFTER_HOURS,
    check_release_freshness,
)
from fpl_model.validation.release_manifest import build_release_manifest


@dataclass(frozen=True, slots=True)
class ReleaseOrchestrationReport:
    report: dict[str, Any]

    @property
    def passes(self) -> bool:
        return bool(self.report["passes"])

    @property
    def approval_status(self) -> str:
        return str(self.report["approval_status"])


def orchestrate_release_validation(
    *,
    model_run_ids: tuple[str, ...],
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
) -> ReleaseOrchestrationReport:
    """Run the manifest, freshness, and approval gates against one release.

    Every check receives the same ``model_run_ids`` and ``database_path``, so a
    single mistyped or stale run ID is caught consistently by all three rather
    than passing one check on a different run set than another. Each check's
    own errors (unknown run ID, duplicate IDs, empty input) propagate unchanged
    -- this function adds no new validation of its own, only composition.

    ``passes`` requires manifest linkage AND freshness to both pass; approval is
    tracked and reported separately via ``approval_status`` (``"approved"`` or
    ``"shadow_only"``) rather than folded into the combined boolean, because
    every artifact in this database is expected to be shadow-only today (see
    `docs/SPRINT4_UNCERTAINTY_AND_CALIBRATION.md`) -- collapsing it into
    ``passes`` would make this command report failure for every release the
    manifest and freshness checks already consider healthy, hiding the two
    checks that actually matter for day-to-day operation behind a gate nothing
    can pass yet. `docs/PIPELINE_ARCHITECTURE.md` and the Sprint 5 checklist
    make the approval requirement explicit at the point a release is actually
    promoted out of `RESEARCH_ONLY`, rather than here.
    """
    manifest = build_release_manifest(model_run_ids=model_run_ids, database_path=database_path)
    freshness = check_release_freshness(
        model_run_ids=model_run_ids,
        database_path=database_path,
        stale_after_hours=stale_after_hours,
    )
    approval = check_release_approval(model_run_ids=model_run_ids, database_path=database_path)

    passes = manifest.passes_linkage_gate and freshness.passes
    approval_status = "approved" if approval.passes else "shadow_only"

    payload: dict[str, Any] = {
        "label": "release_orchestration_v1",
        "model_run_ids": list(model_run_ids),
        "passes": passes,
        "approval_status": approval_status,
        "manifest": manifest.report,
        "freshness": freshness.report,
        "approval": approval.report,
        "limitations": [
            "This command is validate-only: it materialises and writes nothing. Every named "
            "model_run_id must already exist, produced by the ordinary project_*/refresh_* "
            "pipeline commands.",
            "'passes' requires manifest linkage and freshness only. approval_status is reported "
            "separately and is expected to read 'shadow_only' for every release today -- see "
            "docs/SPRINT4_UNCERTAINTY_AND_CALIBRATION.md. A release is not yet approved for "
            "production simply because 'passes' is true.",
            "The 100%-shortlist decision_coverage gate is NOT included here -- it belongs to "
            "each decision command's own output, not to a release considered on its own.",
        ],
    }
    return ReleaseOrchestrationReport(report=payload)


class ReleaseGateFailure(RuntimeError):
    """Raised by ``enforce_release_gate`` when a release fails manifest/freshness.

    Carries the full ``ReleaseOrchestrationReport`` so a caller (typically a
    decision CLI's ``main()``) can print every problem rather than only this
    exception's summary message.
    """

    def __init__(self, result: ReleaseOrchestrationReport) -> None:
        self.result = result
        problems = (
            result.report["manifest"]["linkage"]["problems"]
            + result.report["freshness"]["problems"]
        )
        super().__init__(
            "release fails the manifest/freshness gate for "
            f"model_run_ids={result.report['model_run_ids']}: {'; '.join(problems)}"
        )


def enforce_release_gate(
    *,
    model_run_ids: tuple[str, ...],
    database_path: str | Path = DEFAULT_DATABASE_PATH,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
) -> ReleaseOrchestrationReport:
    """Run ``orchestrate_release_validation`` and raise unless it passes.

    Sprint 6's "consume only an approved Sprint 5 release" requirement is
    interpreted here as the manifest+freshness gate (``passes``), not literal
    artifact ``status='approved'`` -- every artifact is expected to be
    ``shadow_only`` today, so requiring literal approval would make every
    decision command unusable before Sprint 4's confirmatory evaluation exists.
    ``approval_status`` remains visible on the returned report for any caller
    that wants to surface it, and a full research/shadow/production label is
    available via ``validation.release_health.determine_release_health``.

    Raises ``ReleaseGateFailure`` (carrying the full report) when the gate
    fails. Decision CLIs are expected to call this before producing a
    recommendation and to offer an explicit, loudly-labelled
    ``--skip-release-validation`` escape hatch for local development, mirroring
    the existing ``--allow-provisional`` precedent in
    ``scripts/refresh_fpl_event_live.py``.
    """
    result = orchestrate_release_validation(
        model_run_ids=model_run_ids,
        database_path=database_path,
        stale_after_hours=stale_after_hours,
    )
    if not result.passes:
        raise ReleaseGateFailure(result)
    return result
