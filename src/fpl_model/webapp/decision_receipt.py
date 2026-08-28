"""Reproducible, privacy-minimised receipts for web recommendations."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from fpl_model.webapp.alpha_operations import PRIVACY_NOTICE_VERSION, TERMS_VERSION

DECISION_RECEIPT_VERSION = "decision_receipt_v1"


def _sha256(value: Any) -> str:
    canonical = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def build_decision_receipt(
    *,
    decision_type: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
    issued_at: datetime | None = None,
) -> dict[str, Any]:
    """Describe one decision without echoing its squad or settings.

    The stable ID excludes ``issued_at``: the same frozen release, exact
    request, and exact response therefore reproduce the same decision ID.
    """

    release_id = response_payload.get("release_id")
    release_health = response_payload.get("health")
    horizon = response_payload.get("horizon")
    if not isinstance(release_id, str) or not release_id:
        raise ValueError("a decision receipt requires a release_id")
    if not isinstance(release_health, str) or not release_health:
        raise ValueError("a decision receipt requires release health")
    if not isinstance(horizon, list) or not horizon:
        raise ValueError("a decision receipt requires a planning horizon")

    input_digest = _sha256(request_payload)
    output_digest = _sha256(response_payload)
    identity_digest = _sha256(
        {
            "contract": DECISION_RECEIPT_VERSION,
            "decision_type": decision_type,
            "release_id": release_id,
            "privacy_notice_version": PRIVACY_NOTICE_VERSION,
            "terms_version": TERMS_VERSION,
            "input_sha256": input_digest,
            "output_sha256": output_digest,
        }
    )
    observed_at = issued_at or datetime.now(UTC)
    if observed_at.tzinfo is None:
        raise ValueError("decision receipt issued_at must be timezone-aware")
    return {
        "contract": DECISION_RECEIPT_VERSION,
        "decision_id": f"decision_{identity_digest[:20]}",
        "decision_type": decision_type,
        "issued_at": observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "release_id": release_id,
        "release_health": release_health,
        "horizon": horizon,
        "privacy_notice_version": PRIVACY_NOTICE_VERSION,
        "terms_version": TERMS_VERSION,
        "input_sha256": input_digest,
        "output_sha256": output_digest,
        "server_persisted": False,
        "explanation": (
            "This receipt fingerprints the exact request and response against one immutable "
            "release. It contains no squad list, Team ID, selling prices, or access code."
        ),
    }


def attach_decision_receipt(
    response_payload: dict[str, Any],
    *,
    decision_type: str,
    request_payload: dict[str, Any],
    issued_at: datetime | None = None,
) -> dict[str, Any]:
    """Return a copy carrying a receipt calculated before receipt attachment."""

    result = dict(response_payload)
    result["decision_receipt"] = build_decision_receipt(
        decision_type=decision_type,
        request_payload=request_payload,
        response_payload=response_payload,
        issued_at=issued_at,
    )
    return result
