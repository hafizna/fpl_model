"""Small, explicit access boundary for a controlled single-instance alpha.

This is deliberately not an account, entitlement, or payment system.  It lets
an operator issue a different high-entropy code to each tester while storing
only SHA-256 digests server-side, and bounds work per process/identity.  A
public or horizontally scaled service still needs provider-global rate limits
and real authentication.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import threading
import time
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass

TOKEN_HEADER = "X-FPL-Alpha-Token"
TOKEN_HASH_ENV = "FPL_ALPHA_ACCESS_TOKEN_HASHES"
_LABEL = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def hash_access_token(token: str) -> str:
    """Return the lowercase SHA-256 digest used in operator configuration."""

    if len(token) < 16:
        raise ValueError("alpha access tokens must contain at least 16 characters")
    if len(token) > 256:
        raise ValueError("alpha access tokens must contain at most 256 characters")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _env_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("FPL_REQUIRE_ALPHA_ACCESS must be true or false")


def _positive_limit(environment: Mapping[str, str], name: str, *, default: int) -> int:
    raw = environment.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= value <= 10_000:
        raise ValueError(f"{name} must be between 1 and 10000")
    return value


def _configured_digests(raw: str | None) -> tuple[tuple[str, str], ...]:
    if raw is None or not raw.strip():
        return ()
    entries: list[tuple[str, str]] = []
    seen_labels: set[str] = set()
    seen_digests: set[str] = set()
    for index, item in enumerate(raw.split(","), start=1):
        value = item.strip()
        if not value:
            continue
        if "=" in value:
            label, digest = (part.strip() for part in value.split("=", 1))
        else:
            label, digest = f"tester_{index}", value
        digest = digest.lower()
        if not _LABEL.fullmatch(label):
            raise ValueError("alpha access labels must use 1-32 letters, numbers, _ or -")
        if not _SHA256.fullmatch(digest):
            raise ValueError(f"alpha access digest for {label!r} must be lowercase SHA-256 hex")
        if label in seen_labels or digest in seen_digests:
            raise ValueError("alpha access labels and digests must be unique")
        seen_labels.add(label)
        seen_digests.add(digest)
        entries.append((label, digest))
    return tuple(entries)


@dataclass(frozen=True)
class AlphaIdentity:
    label: str
    digest: str


@dataclass(frozen=True)
class AlphaAccessConfig:
    required: bool
    identities: tuple[AlphaIdentity, ...]
    requests_per_minute: int
    transfer_scans_per_minute: int

    @property
    def enabled(self) -> bool:
        return bool(self.identities)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> AlphaAccessConfig:
        configured = _configured_digests(environment.get(TOKEN_HASH_ENV))
        return cls(
            required=_env_bool(environment.get("FPL_REQUIRE_ALPHA_ACCESS"), default=False),
            identities=tuple(AlphaIdentity(label, digest) for label, digest in configured),
            requests_per_minute=_positive_limit(
                environment, "FPL_ALPHA_REQUESTS_PER_MINUTE", default=60
            ),
            transfer_scans_per_minute=_positive_limit(
                environment, "FPL_ALPHA_TRANSFER_SCANS_PER_MINUTE", default=2
            ),
        )

    def authenticate(self, token: str | None) -> AlphaIdentity | None:
        if token is None or not 16 <= len(token) <= 256:
            return None
        candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched: AlphaIdentity | None = None
        # Compare every configured digest so the position of a matching tester
        # does not create an avoidable early-return timing difference.
        for identity in self.identities:
            if hmac.compare_digest(candidate, identity.digest):
                matched = identity
        return matched


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class ProcessRateLimiter:
    """Thread-safe fixed-window queue limiter for one web process."""

    def __init__(self, *, window_seconds: float = 60.0) -> None:
        self.window_seconds = window_seconds
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(
        self,
        identity_digest: str,
        bucket: str,
        *,
        limit: int,
        now: float | None = None,
    ) -> RateLimitDecision:
        observed_at = time.monotonic() if now is None else now
        key = (identity_digest, bucket)
        with self._lock:
            events = self._events[key]
            cutoff = observed_at - self.window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                retry_after = max(1, math.ceil(self.window_seconds - (observed_at - events[0])))
                return RateLimitDecision(False, limit, 0, retry_after)
            events.append(observed_at)
            return RateLimitDecision(True, limit, limit - len(events), 0)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
