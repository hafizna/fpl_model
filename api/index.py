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
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from fpl_model.ingest.fpl import FPLClient
from fpl_model.storage import DEFAULT_DATABASE_PATH
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
    allow_headers=["Content-Type"],
)


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


@app.get("/api/squad/from-entry/{entry_id}")
def squad_from_entry(entry_id: int, gameweek: int | None = None) -> dict[str, object]:
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

    client = FPLClient()
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

    return {"entry_id": entry_id, "gameweek": target_gameweek, **resolved}


@app.post("/api/recommend/lineups")
def lineups(request: SquadRequest) -> dict[str, object]:
    try:
        return recommend_web_lineups(
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
        return recommend_web_transfers(
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
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


@app.get("/{asset_name}")
def static_asset(asset_name: str) -> FileResponse:
    if asset_name not in {"app.js", "styles.css"}:
        raise HTTPException(status_code=404)
    return FileResponse(WEB_ROOT / asset_name)
