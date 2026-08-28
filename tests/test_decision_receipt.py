from __future__ import annotations

from datetime import UTC, datetime

from fpl_model.webapp.decision_receipt import attach_decision_receipt, build_decision_receipt


def _response() -> dict:
    return {
        "release_id": "release_test",
        "health": "shadow",
        "horizon": [2, 3, 4],
        "lineups": [{"gameweek": 2, "total_xpts": 62.5}],
    }


def test_receipt_is_stable_for_the_same_release_request_and_response():
    request = {"fpl_ids": list(range(1, 16)), "bank_tenths": 5}

    first = build_decision_receipt(
        decision_type="lineup_outlook",
        request_payload=request,
        response_payload=_response(),
        issued_at=datetime(2026, 8, 28, 1, tzinfo=UTC),
    )
    second = build_decision_receipt(
        decision_type="lineup_outlook",
        request_payload=request,
        response_payload=_response(),
        issued_at=datetime(2026, 8, 28, 2, tzinfo=UTC),
    )

    assert first["decision_id"] == second["decision_id"]
    assert first["issued_at"] != second["issued_at"]
    assert first["server_persisted"] is False
    assert first["privacy_notice_version"].startswith("closed_alpha_privacy_")
    assert first["terms_version"].startswith("closed_alpha_terms_")


def test_receipt_changes_when_an_input_or_output_changes():
    base = build_decision_receipt(
        decision_type="lineup_outlook",
        request_payload={"fpl_ids": list(range(1, 16))},
        response_payload=_response(),
    )
    changed_input = build_decision_receipt(
        decision_type="lineup_outlook",
        request_payload={"fpl_ids": list(range(2, 17))},
        response_payload=_response(),
    )
    changed_response_payload = _response()
    changed_response_payload["lineups"][0]["total_xpts"] = 63.0
    changed_output = build_decision_receipt(
        decision_type="lineup_outlook",
        request_payload={"fpl_ids": list(range(1, 16))},
        response_payload=changed_response_payload,
    )

    assert len({base["decision_id"], changed_input["decision_id"], changed_output["decision_id"]}) == 3


def test_attached_receipt_does_not_echo_private_request_fields():
    response = _response()
    result = attach_decision_receipt(
        response,
        decision_type="transfer",
        request_payload={
            "fpl_ids": [101, 202],
            "selling_prices": {101: 75},
            "alpha_token": "must-never-appear",
        },
    )

    receipt_text = str(result["decision_receipt"])
    assert "must-never-appear" not in receipt_text
    assert "101" not in receipt_text
    assert result["decision_receipt"]["release_id"] == "release_test"
    assert "decision_receipt" not in response
