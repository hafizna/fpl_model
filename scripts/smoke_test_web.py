"""Fail closed unless a running web app serves one internally consistent release."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any
from urllib.request import Request, urlopen

from fpl_model.webapp.alpha_access import TOKEN_HEADER


def _get_json(
    base_url: str,
    path: str,
    *,
    timeout_seconds: float,
    access_token: str | None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "fpl-runtime-smoke/1"}
    if access_token is not None:
        headers[TOKEN_HEADER] = access_token
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        headers=headers,
    )
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - operator URL
        if response.status != 200:
            raise ValueError(f"{path} returned HTTP {response.status}")
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} did not return a JSON object")
    return payload


def check_web_runtime(
    base_url: str,
    *,
    expected_release_id: str | None = None,
    allowed_health: frozenset[str] = frozenset({"shadow", "production"}),
    timeout_seconds: float = 10.0,
    access_token: str | None = None,
) -> dict[str, Any]:
    """Verify liveness, readiness, and bootstrap all describe the same release."""

    live = _get_json(
        base_url, "/api/live", timeout_seconds=timeout_seconds, access_token=access_token
    )
    ready = _get_json(
        base_url, "/api/ready", timeout_seconds=timeout_seconds, access_token=access_token
    )
    bootstrap = _get_json(
        base_url, "/api/bootstrap", timeout_seconds=timeout_seconds, access_token=access_token
    )

    release = bootstrap.get("release")
    players = bootstrap.get("players")
    if live.get("ok") is not True:
        raise ValueError("liveness response is not healthy")
    if ready.get("ready") is not True:
        raise ValueError("readiness response is not ready")
    if not isinstance(release, dict) or not isinstance(players, list):
        raise ValueError("bootstrap response lacks release or player catalog")

    release_id = ready.get("release_id")
    health = ready.get("release_health")
    horizon = ready.get("horizon")
    bootstrap_horizon = [row.get("gameweek") for row in release.get("model_runs", [])]
    if not isinstance(release_id, str) or release.get("release_id") != release_id:
        raise ValueError("readiness and bootstrap release IDs differ")
    if expected_release_id is not None and release_id != expected_release_id:
        raise ValueError(f"expected release {expected_release_id!r}, received {release_id!r}")
    if health not in allowed_health or release.get("health") != health:
        raise ValueError(f"release health {health!r} is not allowed or internally consistent")
    if horizon != bootstrap_horizon or len(horizon or []) != 3:
        raise ValueError("readiness and bootstrap must expose the same three-Gameweek horizon")
    if ready.get("catalog_players") != len(players) or not players:
        raise ValueError("readiness and bootstrap catalog sizes differ or are empty")
    if ready.get("rating_benchmark_status") != "ready":
        raise ValueError("materialized squad-rating benchmark is not ready")
    if ready.get("alpha_access_required") is True and ready.get("alpha_operations_ready") is not True:
        raise ValueError("closed-alpha operator/privacy boundary is not ready")

    return {
        "contract": "web_runtime_smoke_v1",
        "passes": True,
        "base_url": base_url.rstrip("/"),
        "release_id": release_id,
        "release_health": health,
        "horizon": horizon,
        "catalog_players": len(players),
        "rating_benchmark_id": ready.get("rating_benchmark_id"),
        "alpha_access_required": ready.get("alpha_access_required", False),
        "alpha_operations_ready": ready.get("alpha_operations_ready", False),
        "privacy_notice_version": ready.get("privacy_notice_version"),
        "terms_version": ready.get("terms_version"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--expected-release-id")
    parser.add_argument(
        "--allowed-health",
        default="shadow,production",
        help="Comma-separated release health states accepted by this environment.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument(
        "--access-token-env",
        default="FPL_ALPHA_ACCESS_TOKEN",
        help="Environment variable containing the plaintext tester code; never pass it as an argument.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    allowed_health = frozenset(
        value.strip().lower() for value in args.allowed_health.split(",") if value.strip()
    )
    if not allowed_health:
        raise SystemExit("--allowed-health must contain at least one state")
    try:
        report = check_web_runtime(
            args.base_url,
            expected_release_id=args.expected_release_id,
            allowed_health=allowed_health,
            timeout_seconds=args.timeout_seconds,
            access_token=os.environ.get(args.access_token_env),
        )
    except Exception as exc:  # one machine-readable boundary for deployment automation
        print(
            json.dumps(
                {
                    "contract": "web_runtime_smoke_v1",
                    "passes": False,
                    "base_url": args.base_url.rstrip("/"),
                    "error": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
        )
        raise SystemExit(2) from exc
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
