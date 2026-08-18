"""Verify a re-run's aggregate backtest metrics against a reference JSON.

Extracted from ``scripts/diagnose_backtest_segments.py`` into an importable
module (mirroring ``matched_naive.py``) so the comparison logic has proper
unit test coverage instead of living only in a non-package script.
"""

from __future__ import annotations

import math
from pathlib import Path

from fpl_model.validation.backtest import BacktestMetrics

# Floating-point metrics are recomputed from the same observations via the
# same score_predictions() call the production run used, so any difference
# should be exactly zero; this tolerance only absorbs machine-level summation
# order/rounding noise across separate Python process runs, not a real
# methodology drift.
FLOAT_METRIC_TOLERANCE = 1e-9

EXACT_REFERENCE_FIELDS = ("import_run_id", "evaluation_from_gw", "evaluation_to_gw")
FLOAT_REFERENCE_METRICS = (
    "observations",
    "mean_absolute_error",
    "root_mean_squared_error",
    "mean_error",
)


def load_reference(reference_path: Path) -> dict[str, object]:
    import json

    if not reference_path.is_file():
        raise ValueError(
            f"reference JSON not found: {reference_path} -- run "
            "scripts/backtest_benchwarmers.py first, or pass --reference explicitly"
        )
    return json.loads(reference_path.read_text(encoding="utf-8"))


def verify_self_check(
    *,
    reference: dict[str, object],
    reference_path: Path | str,
    import_run_id: str,
    evaluation_from_gw: int,
    evaluation_to_gw: int,
    model_metrics: BacktestMetrics,
) -> dict[str, object]:
    """Compare a re-run's recomputed aggregate metrics against ``reference``.

    Raises ``ValueError`` with every mismatch listed if any field disagrees;
    the caller must not treat the run as verified when this raises. Exact
    fields (identity/config) use ``==``; floating-point metrics use
    ``math.isclose`` with ``FLOAT_METRIC_TOLERANCE`` (documented above) since
    they are recomputed independently rather than copied from the reference.
    """
    actual: dict[str, object] = {
        "import_run_id": import_run_id,
        "evaluation_from_gw": evaluation_from_gw,
        "evaluation_to_gw": evaluation_to_gw,
        "observations": model_metrics.observations,
        "mean_absolute_error": model_metrics.mean_absolute_error,
        "root_mean_squared_error": model_metrics.root_mean_squared_error,
        "mean_error": model_metrics.mean_error,
    }
    if "metrics" not in reference:
        raise ValueError(f"reference JSON {reference_path} has no 'metrics' field")
    reference_metrics = reference["metrics"]

    mismatches: list[str] = []
    for field in EXACT_REFERENCE_FIELDS:
        if field not in reference:
            mismatches.append(f"{field}: missing from reference JSON")
            continue
        expected = reference[field]
        if actual[field] != expected:
            mismatches.append(f"{field}: expected {expected!r}, got {actual[field]!r}")
    for field in FLOAT_REFERENCE_METRICS:
        if field not in reference_metrics:
            mismatches.append(f"metrics.{field}: missing from reference JSON")
            continue
        expected = reference_metrics[field]
        got = actual[field]
        if field == "observations":
            if got != expected:
                mismatches.append(f"metrics.{field}: expected {expected!r}, got {got!r}")
        elif not math.isclose(got, expected, rel_tol=0.0, abs_tol=FLOAT_METRIC_TOLERANCE):
            mismatches.append(
                f"metrics.{field}: expected {expected!r}, got {got!r} "
                f"(abs diff {abs(got - expected):.3e} > tolerance {FLOAT_METRIC_TOLERANCE:.0e})"
            )

    if mismatches:
        joined = "\n  - ".join(mismatches)
        raise ValueError(
            f"self-check against reference {reference_path} failed -- this run's "
            "recomputed aggregate metrics diverge from the production backtest, "
            "so no segment breakdown was written:\n  - " + joined
        )

    return {
        "reference_path": str(reference_path),
        "checked_fields": list(EXACT_REFERENCE_FIELDS)
        + [f"metrics.{f}" for f in FLOAT_REFERENCE_METRICS],
        "float_tolerance": FLOAT_METRIC_TOLERANCE,
        "status": "passed",
    }
