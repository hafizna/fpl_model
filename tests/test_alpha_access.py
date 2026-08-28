from __future__ import annotations

import pytest

from fpl_model.webapp.alpha_access import (
    AlphaAccessConfig,
    ProcessRateLimiter,
    hash_access_token,
)


def test_config_authenticates_named_hashed_testers_without_plaintext_storage():
    alice_token = "alice-alpha-code-123456"
    bob_token = "bob-alpha-code-12345678"
    config = AlphaAccessConfig.from_environment(
        {
            "FPL_REQUIRE_ALPHA_ACCESS": "true",
            "FPL_ALPHA_ACCESS_TOKEN_HASHES": (
                f"alice={hash_access_token(alice_token)},bob={hash_access_token(bob_token)}"
            ),
        }
    )

    assert config.required is True
    assert config.enabled is True
    assert config.authenticate(alice_token).label == "alice"
    assert config.authenticate(bob_token).label == "bob"
    assert config.authenticate("wrong-alpha-code-1234") is None
    assert all(alice_token not in identity.digest for identity in config.identities)


def test_disabled_config_is_valid_for_local_development():
    config = AlphaAccessConfig.from_environment({})

    assert config.required is False
    assert config.enabled is False
    assert config.requests_per_minute == 60
    assert config.transfer_scans_per_minute == 2


@pytest.mark.parametrize(
    "environment,match",
    [
        ({"FPL_REQUIRE_ALPHA_ACCESS": "sometimes"}, "true or false"),
        ({"FPL_ALPHA_ACCESS_TOKEN_HASHES": "alice=not-a-digest"}, "SHA-256"),
        ({"FPL_ALPHA_REQUESTS_PER_MINUTE": "0"}, "between 1 and 10000"),
        ({"FPL_ALPHA_TRANSFER_SCANS_PER_MINUTE": "many"}, "must be an integer"),
    ],
)
def test_config_fails_closed_on_invalid_operator_values(environment: dict[str, str], match: str):
    with pytest.raises(ValueError, match=match):
        AlphaAccessConfig.from_environment(environment)


def test_process_rate_limiter_is_per_identity_and_bucket():
    limiter = ProcessRateLimiter(window_seconds=60)

    assert limiter.check("alice", "general", limit=2, now=100).allowed is True
    second = limiter.check("alice", "general", limit=2, now=101)
    rejected = limiter.check("alice", "general", limit=2, now=102)

    assert second.remaining == 0
    assert rejected.allowed is False
    assert rejected.retry_after_seconds == 58
    assert limiter.check("bob", "general", limit=2, now=102).allowed is True
    assert limiter.check("alice", "transfer", limit=2, now=102).allowed is True
    assert limiter.check("alice", "general", limit=2, now=161).allowed is True


@pytest.mark.parametrize("token", ["short", "x" * 257])
def test_hash_access_token_rejects_weak_or_unbounded_values(token: str):
    with pytest.raises(ValueError, match="characters"):
        hash_access_token(token)
