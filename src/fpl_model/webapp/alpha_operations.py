"""Public operator/support metadata required before a real closed alpha."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

PRIVACY_NOTICE_VERSION = "closed_alpha_privacy_2026-08-28_v1"
TERMS_VERSION = "closed_alpha_terms_2026-08-28_v1"
OFFICIAL_PDP_LAW_URL = "https://jdih.komdigi.go.id/produk_hukum/view/id/832/t/crc32/"
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _optional_text(environment: Mapping[str, str], name: str, *, maximum: int) -> str | None:
    raw = environment.get(name)
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    if len(value) > maximum:
        raise ValueError(f"{name} must contain at most {maximum} characters")
    return value


def _reviewed(environment: Mapping[str, str]) -> bool:
    raw = environment.get("FPL_LEGAL_NOTICE_REVIEWED")
    if raw is None:
        return False
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("FPL_LEGAL_NOTICE_REVIEWED must be true or false")


def _retention_days(environment: Mapping[str, str]) -> int | None:
    raw = environment.get("FPL_LOG_RETENTION_DAYS")
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("FPL_LOG_RETENTION_DAYS must be an integer") from exc
    if not 1 <= value <= 365:
        raise ValueError("FPL_LOG_RETENTION_DAYS must be between 1 and 365")
    return value


@dataclass(frozen=True)
class AlphaOperationsConfig:
    operator_name: str | None
    support_email: str | None
    hosting_provider: str | None
    hosting_region: str | None
    log_retention_days: int | None
    legal_notice_reviewed: bool

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> AlphaOperationsConfig:
        support_email = _optional_text(environment, "FPL_SUPPORT_EMAIL", maximum=254)
        if support_email is not None and not _EMAIL.fullmatch(support_email):
            raise ValueError("FPL_SUPPORT_EMAIL must be a valid email address")
        return cls(
            operator_name=_optional_text(environment, "FPL_OPERATOR_NAME", maximum=100),
            support_email=support_email,
            hosting_provider=_optional_text(environment, "FPL_HOSTING_PROVIDER", maximum=100),
            hosting_region=_optional_text(environment, "FPL_HOSTING_REGION", maximum=100),
            log_retention_days=_retention_days(environment),
            legal_notice_reviewed=_reviewed(environment),
        )

    @property
    def problems(self) -> tuple[str, ...]:
        missing = []
        for name, value in (
            ("FPL_OPERATOR_NAME", self.operator_name),
            ("FPL_SUPPORT_EMAIL", self.support_email),
            ("FPL_HOSTING_PROVIDER", self.hosting_provider),
            ("FPL_HOSTING_REGION", self.hosting_region),
            ("FPL_LOG_RETENTION_DAYS", self.log_retention_days),
        ):
            if value is None:
                missing.append(f"{name} is not configured")
        if not self.legal_notice_reviewed:
            missing.append("FPL_LEGAL_NOTICE_REVIEWED is not true")
        return tuple(missing)

    @property
    def ready(self) -> bool:
        return not self.problems

    def public_payload(self) -> dict[str, object]:
        return {
            "contract": "closed_alpha_operations_v1",
            "ready": self.ready,
            "operator_name": self.operator_name,
            "support_email": self.support_email,
            "hosting_provider": self.hosting_provider,
            "hosting_region": self.hosting_region,
            "log_retention_days": self.log_retention_days,
            "legal_notice_reviewed": self.legal_notice_reviewed,
            "privacy_notice_version": PRIVACY_NOTICE_VERSION,
            "terms_version": TERMS_VERSION,
            "official_pdp_law_url": OFFICIAL_PDP_LAW_URL,
            "data_boundary": {
                "server_side_squad_storage": False,
                "accounts_or_payments": False,
                "analytics_or_advertising_cookies": False,
                "browser_squad_storage": "localStorage",
                "browser_access_code_storage": "sessionStorage",
                "recommendation_receipts_server_persisted": False,
            },
        }
