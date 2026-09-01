"""FastAPI adapter for the browser recommender and Vercel Python runtime."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from functools import lru_cache
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from fpl_model.ingest.fpl import FPLClient
from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.webapp.alpha_access import (
    TOKEN_HEADER,
    AlphaAccessConfig,
    ProcessRateLimiter,
)
from fpl_model.webapp.alpha_operations import AlphaOperationsConfig
from fpl_model.webapp.decision_receipt import attach_decision_receipt
from fpl_model.webapp.service import (
    CurrentSquadSetup,
    RoleScenarioOverride,
    load_web_bootstrap,
    recommend_web_lineups,
    recommend_web_transfers,
    resolve_entry_picks,
)

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
LOGGER = logging.getLogger("fpl_model.web")
configured_log_level = os.environ.get("FPL_LOG_LEVEL", "INFO").upper()
numeric_log_level = getattr(logging, configured_log_level, None)
if not isinstance(numeric_log_level, int):
    raise ValueError("FPL_LOG_LEVEL must be a standard Python logging level")
LOGGER.setLevel(numeric_log_level)
ALPHA_RATE_LIMITER = ProcessRateLimiter()


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _allowed_origins() -> list[str]:
    configured = os.environ.get("FPL_ALLOWED_ORIGINS")
    if configured is None:
        return ["http://localhost:3000", "http://127.0.0.1:3000"]
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


def _allowed_release_health() -> set[str]:
    default = "shadow,production" if os.environ.get("VERCEL") == "1" else "research,shadow,production"
    configured = os.environ.get(
        "FPL_ALLOWED_RELEASE_HEALTH",
        default,
    )
    values = {value.strip().lower() for value in configured.split(",") if value.strip()}
    supported = {"research", "shadow", "production"}
    if not values or not values <= supported:
        raise ValueError(
            "FPL_ALLOWED_RELEASE_HEALTH must contain research, shadow, and/or production"
        )
    return values


def _database_path() -> Path:
    configured = os.environ.get("FPL_DATABASE_PATH")
    return DEFAULT_DATABASE_PATH if configured is None else Path(configured)


def _release_path() -> Path | None:
    configured = os.environ.get("FPL_WEB_RELEASE_PATH")
    candidate = WEB_ROOT / "release.json" if configured is None else Path(configured)
    return candidate if candidate.is_file() else None


class RoleScenarioOverrideRequest(BaseModel):
    """One reviewed 'treat this player's this-Gameweek xPts as X' override.

    See `webapp.service.RoleScenarioOverride`: applied entirely in memory
    for one request, never written back to the release file or a database.
    """

    fpl_id: int = Field(gt=0)
    gameweek: int = Field(ge=1, le=38)
    xpts: float = Field(ge=0.0)

    def to_override(self) -> RoleScenarioOverride:
        return RoleScenarioOverride(fpl_id=self.fpl_id, gameweek=self.gameweek, xpts=self.xpts)


class CurrentSetupRequest(BaseModel):
    gameweek: int = Field(ge=1, le=38)
    starter_fpl_ids: list[int] = Field(min_length=11, max_length=11)
    bench_fpl_ids: list[int] = Field(min_length=4, max_length=4)
    captain_fpl_id: int = Field(gt=0)
    vice_captain_fpl_id: int = Field(gt=0)

    def to_setup(self) -> CurrentSquadSetup:
        return CurrentSquadSetup(
            gameweek=self.gameweek,
            starter_fpl_ids=tuple(self.starter_fpl_ids),
            bench_fpl_ids=tuple(self.bench_fpl_ids),
            captain_fpl_id=self.captain_fpl_id,
            vice_captain_fpl_id=self.vice_captain_fpl_id,
        )


class SquadRequest(BaseModel):
    fpl_ids: list[int] = Field(min_length=15, max_length=15)
    bank_tenths: int = Field(default=0, ge=0)
    free_transfers: int = Field(default=1, ge=0, le=5)
    selling_prices: dict[int, int] = Field(default_factory=dict)
    role_scenario_overrides: list[RoleScenarioOverrideRequest] = Field(default_factory=list)
    current_setup: CurrentSetupRequest | None = None


expose_api_docs = _env_bool(
    "FPL_EXPOSE_API_DOCS",
    default=os.environ.get("VERCEL") != "1",
)
app = FastAPI(
    title="FPL Model Web API",
    version="0.1.0",
    description="Research-only browser boundary over the deterministic FPL decision engine.",
    docs_url="/docs" if expose_api_docs else None,
    redoc_url="/redoc" if expose_api_docs else None,
    openapi_url="/openapi.json" if expose_api_docs else None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", TOKEN_HEADER],
)


def _alpha_access_config() -> AlphaAccessConfig:
    return AlphaAccessConfig.from_environment(os.environ)


def _alpha_operations_config() -> AlphaOperationsConfig:
    return AlphaOperationsConfig.from_environment(os.environ)


def _protected_alpha_path(path: str) -> bool:
    return path == "/api/bootstrap" or path.startswith(("/api/squad/", "/api/recommend/"))


def _private_alpha_response(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = TOKEN_HEADER
    return response


def _request_payload(request: SquadRequest) -> dict[str, object]:
    return request.model_dump(mode="json")


def _attach_and_log_receipt(
    payload: dict[str, object],
    *,
    request: SquadRequest,
    decision_type: str,
) -> dict[str, object]:
    result = attach_decision_receipt(
        payload,
        decision_type=decision_type,
        request_payload=_request_payload(request),
    )
    receipt = result["decision_receipt"]
    LOGGER.info(
        json.dumps(
            {
                "event": "decision_receipt",
                "decision_id": receipt["decision_id"],
                "decision_type": receipt["decision_type"],
                "release_id": receipt["release_id"],
                "release_health": receipt["release_health"],
                "server_persisted": False,
            },
            separators=(",", ":"),
        )
    )
    return result


@app.middleware("http")
async def alpha_access_control(request: Request, call_next):
    """Gate decision data and bound per-tester work during a controlled alpha."""

    if not _protected_alpha_path(request.url.path):
        return await call_next(request)
    try:
        config = _alpha_access_config()
    except ValueError:
        LOGGER.exception("invalid closed-alpha access configuration")
        return _private_alpha_response(
            JSONResponse(
                status_code=503,
                content={
                    "detail": "closed-alpha access is unavailable due to operator configuration"
                },
            )
        )
    if config.required:
        try:
            alpha_operations = _alpha_operations_config()
        except ValueError:
            LOGGER.exception("invalid closed-alpha operator/privacy configuration")
            return _private_alpha_response(
                JSONResponse(
                    status_code=503,
                    content={"detail": "closed-alpha operator/privacy boundary is unavailable"},
                )
            )
        if not alpha_operations.ready:
            return _private_alpha_response(
                JSONResponse(
                    status_code=503,
                    content={"detail": "closed-alpha operator/privacy boundary is not ready"},
                )
            )
    if not config.enabled:
        if config.required:
            return _private_alpha_response(
                JSONResponse(
                    status_code=503,
                    content={"detail": "closed-alpha access is required but no tester codes exist"},
                )
            )
        return _private_alpha_response(await call_next(request))

    identity = config.authenticate(request.headers.get(TOKEN_HEADER))
    if identity is None:
        return _private_alpha_response(
            JSONResponse(
                status_code=401,
                content={
                    "detail": "A valid closed-alpha access code is required.",
                    "code": "alpha_access_required",
                },
                headers={"WWW-Authenticate": "FPLAlpha"},
            )
        )

    general = ALPHA_RATE_LIMITER.check(
        identity.digest,
        "general",
        limit=config.requests_per_minute,
    )
    if not general.allowed:
        return _private_alpha_response(
            JSONResponse(
                status_code=429,
                content={"detail": "Too many alpha requests; retry after the indicated delay."},
                headers={"Retry-After": str(general.retry_after_seconds)},
            )
        )
    transfer = None
    if request.url.path == "/api/recommend/transfers":
        transfer = ALPHA_RATE_LIMITER.check(
            identity.digest,
            "transfer",
            limit=config.transfer_scans_per_minute,
        )
        if not transfer.allowed:
            return _private_alpha_response(
                JSONResponse(
                    status_code=429,
                    content={
                        "detail": "Transfer scan limit reached; retry after the indicated delay."
                    },
                    headers={"Retry-After": str(transfer.retry_after_seconds)},
                )
            )

    response = await call_next(request)
    response.headers["X-RateLimit-Scope"] = "process"
    response.headers["X-RateLimit-Limit"] = str(
        transfer.limit if transfer is not None else general.limit
    )
    response.headers["X-RateLimit-Remaining"] = str(
        transfer.remaining if transfer is not None else general.remaining
    )
    return _private_alpha_response(response)


@app.middleware("http")
async def request_observability(request: Request, call_next):
    """Emit one privacy-safe structured record for platform log alerts.

    The route template is logged instead of the raw URL, so a Team ID in
    ``/api/squad/from-entry/{entry_id}`` never lands in application logs.
    """

    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        LOGGER.exception(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "route": "unmatched",
                    "status_code": 500,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
                separators=(",", ":"),
            )
        )
        raise
    route = getattr(request.scope.get("route"), "path", "unmatched")
    LOGGER.info(
        json.dumps(
            {
                "event": "http_request",
                "request_id": request_id,
                "method": request.method,
                "route": route,
                "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            },
            separators=(",", ":"),
        )
    )
    response.headers["X-Request-ID"] = request_id
    return response


@lru_cache(maxsize=1)
def _bootstrap() -> dict[str, object]:
    return load_web_bootstrap(_database_path(), release_path=_release_path())


@app.get("/api/live")
def live() -> dict[str, object]:
    """Cheap process liveness check; does not touch the release artifact."""

    return {"ok": True, "service": "fpl-web-api"}


def _readiness_payload() -> dict[str, object]:
    database_path = _database_path()
    release_path = _release_path()
    bootstrap_payload = _bootstrap()
    release = bootstrap_payload["release"]
    allowed_health = _allowed_release_health()
    alpha_access = _alpha_access_config()
    alpha_operations = _alpha_operations_config()
    if alpha_access.required and not alpha_access.enabled:
        raise ValueError("closed-alpha access is required but no tester codes are configured")
    if alpha_access.required and not alpha_operations.ready:
        raise ValueError(
            "closed-alpha operator/privacy boundary is not ready: "
            + "; ".join(alpha_operations.problems)
        )
    if release["health"] not in allowed_health:
        raise ValueError(
            f"release health {release['health']!r} is not allowed in this environment"
        )
    rating_benchmark = release.get("rating_benchmark")
    if release["health"] == "production" and (
        not isinstance(rating_benchmark, dict)
        or rating_benchmark.get("status") != "ready"
        or rating_benchmark.get("compatible") is not True
    ):
        raise ValueError("production release lacks a ready materialized squad benchmark")
    return {
        "ok": True,
        "ready": True,
        "mode": "compact_release" if release_path is not None else "database",
        "release_id": release["release_id"],
        "release_health": release["health"],
        "planning_as_of": release["planning_as_of"],
        "horizon": [row["gameweek"] for row in release["model_runs"]],
        "catalog_players": len(bootstrap_payload["players"]),
        "database_exists": database_path.exists(),
        "rating_benchmark_status": (
            None if not isinstance(rating_benchmark, dict) else rating_benchmark.get("status")
        ),
        "rating_benchmark_id": (
            None if not isinstance(rating_benchmark, dict) else rating_benchmark.get("artifact_id")
        ),
        "alpha_access_enabled": alpha_access.enabled,
        "alpha_access_required": alpha_access.required,
        "alpha_rate_limit_scope": "process" if alpha_access.enabled else None,
        "alpha_operations_ready": alpha_operations.ready,
        "privacy_notice_version": alpha_operations.public_payload()["privacy_notice_version"],
        "terms_version": alpha_operations.public_payload()["terms_version"],
    }


@app.get("/api/health")
@app.get("/api/ready")
def readiness() -> dict[str, object]:
    """Validate that the pinned release can actually serve decisions."""

    try:
        return _readiness_payload()
    except (ValueError, OSError, KeyError) as exc:
        raise HTTPException(status_code=503, detail=f"decision release is not ready: {exc}") from exc


@app.get("/api/bootstrap")
def bootstrap() -> dict[str, object]:
    try:
        return _bootstrap()
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/public-config")
def public_config() -> dict[str, object]:
    """Expose non-secret operator/legal metadata even before alpha unlock."""

    try:
        return _alpha_operations_config().public_payload()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/squad/from-entry/{entry_id}")
def squad_from_entry(
    entry_id: int,
    gameweek: int | None = None,
    include_profile: bool = False,
) -> dict[str, object]:
    """Load a public squad by FPL Team ID -- no password or session cookie.

    Fetches live from FPL's public `entry/{id}/event/{gw}/picks/` endpoint
    (never `my-team/{id}/`, which is private and requires a login session).
    ``gameweek`` defaults to the current bootstrap horizon's own start
    Gameweek, since a squad for any other Gameweek would fall outside the
    projections this app can currently score. This performs no server-side
    write: the resolved squad is handed straight back to the browser, which
    is the only place squad state is kept (see docs/WEB_APP.md).
    """
    if entry_id <= 0:
        raise HTTPException(status_code=422, detail="entry_id must be positive")
    try:
        release = _bootstrap()["release"]
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    target_gameweek = gameweek or release["model_runs"][0]["gameweek"]

    # Keep the optional profile enrichment bounded; picks remain the critical
    # path and should not wait on a slow profile request for the full client
    # default timeout.
    client = FPLClient(timeout_seconds=10.0)
    profile: dict[str, object] | None = None
    if include_profile:
        try:
            raw_profile = client.entry(entry_id)
            profile = {
                "id": raw_profile.get("id"),
                "name": raw_profile.get("name"),
                "player_first_name": raw_profile.get("player_first_name"),
                "player_last_name": raw_profile.get("player_last_name"),
                "current_event": raw_profile.get("current_event"),
            }
        except (requests.HTTPError, requests.RequestException, ValueError):
            # Profile metadata is a convenience only. A temporary profile
            # failure must not prevent the public picks endpoint from loading.
            profile = None
    try:
        picks_payload = client.entry_picks(entry_id, target_gameweek)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 502
        if status == 404:
            raise HTTPException(
                status_code=404,
                detail=f"no public squad found for Team ID {entry_id} in GW{target_gameweek}",
            ) from exc
        raise HTTPException(status_code=502, detail="FPL's public API is unavailable") from exc
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        resolved = resolve_entry_picks(
            picks_payload,
            database_path=_database_path(),
            release_path=_release_path(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    payload = {"entry_id": entry_id, "gameweek": target_gameweek, **resolved}
    if profile is not None:
        payload["entry"] = profile
    return payload


@app.post("/api/recommend/lineups")
def lineups(request: SquadRequest) -> dict[str, object]:
    try:
        payload = recommend_web_lineups(
            tuple(request.fpl_ids),
            bank_tenths=request.bank_tenths,
            free_transfers=request.free_transfers,
            selling_prices=request.selling_prices,
            role_scenario_overrides=tuple(
                row.to_override() for row in request.role_scenario_overrides
            ),
            current_setup=(
                None if request.current_setup is None else request.current_setup.to_setup()
            ),
            database_path=_database_path(),
            release_path=_release_path(),
        )
        return _attach_and_log_receipt(
            payload,
            request=request,
            decision_type="lineup_outlook",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/recommend/transfers")
def transfers(request: SquadRequest, top_n: int = 8) -> dict[str, object]:
    try:
        transfer_scan_enabled = _env_bool("FPL_TRANSFER_SCAN_ENABLED", default=True)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not transfer_scan_enabled:
        raise HTTPException(status_code=503, detail="transfer scan is disabled by the operator")
    try:
        payload = recommend_web_transfers(
            tuple(request.fpl_ids),
            bank_tenths=request.bank_tenths,
            free_transfers=request.free_transfers,
            selling_prices=request.selling_prices,
            role_scenario_overrides=tuple(
                row.to_override() for row in request.role_scenario_overrides
            ),
            top_n=max(1, min(top_n, 20)),
            database_path=_database_path(),
            release_path=_release_path(),
        )
        return _attach_and_log_receipt(
            payload,
            request=request,
            decision_type="single_transfer_scan",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


@app.get("/privacy")
def privacy() -> FileResponse:
    return FileResponse(WEB_ROOT / "privacy.html")


@app.get("/terms")
def terms() -> FileResponse:
    return FileResponse(WEB_ROOT / "terms.html")


@app.get("/{asset_name}")
def static_asset(asset_name: str) -> FileResponse:
    if asset_name not in {"app.js", "legal.js", "styles.css"}:
        raise HTTPException(status_code=404)
    return FileResponse(WEB_ROOT / asset_name)
