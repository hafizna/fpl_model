from __future__ import annotations

import pytest

from fpl_model.webapp.alpha_operations import AlphaOperationsConfig


def _ready_environment() -> dict[str, str]:
    return {
        "FPL_OPERATOR_NAME": "Touchline Alpha Operator",
        "FPL_SUPPORT_EMAIL": "support@example.test",
        "FPL_HOSTING_PROVIDER": "Example Host",
        "FPL_HOSTING_REGION": "Indonesia",
        "FPL_LOG_RETENTION_DAYS": "14",
        "FPL_LEGAL_NOTICE_REVIEWED": "true",
    }


def test_ready_operations_config_exposes_only_public_metadata():
    config = AlphaOperationsConfig.from_environment(_ready_environment())
    payload = config.public_payload()

    assert config.ready is True
    assert config.problems == ()
    assert payload["support_email"] == "support@example.test"
    assert payload["data_boundary"]["server_side_squad_storage"] is False


def test_missing_metadata_is_explicit_and_not_ready():
    config = AlphaOperationsConfig.from_environment({})

    assert config.ready is False
    assert "FPL_OPERATOR_NAME is not configured" in config.problems
    assert "FPL_LEGAL_NOTICE_REVIEWED is not true" in config.problems


@pytest.mark.parametrize(
    "name,value,match",
    [
        ("FPL_SUPPORT_EMAIL", "not-an-email", "valid email"),
        ("FPL_LOG_RETENTION_DAYS", "0", "between 1 and 365"),
        ("FPL_LOG_RETENTION_DAYS", "forever", "integer"),
        ("FPL_LEGAL_NOTICE_REVIEWED", "maybe", "true or false"),
    ],
)
def test_invalid_operator_metadata_fails_closed(name: str, value: str, match: str):
    environment = _ready_environment()
    environment[name] = value

    with pytest.raises(ValueError, match=match):
        AlphaOperationsConfig.from_environment(environment)
