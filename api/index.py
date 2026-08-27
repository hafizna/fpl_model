"""FastAPI adapter for the browser recommender and Vercel Python runtime."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from fpl_model.ingest.fpl import FPLClient
from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.webapp.service import (
    load_web_bootstrap,
    recommend_web_lineups,
    recommend_web_transfers,
    resolve_entry_picks,
)

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"


def _database_path() -> Path:
    configured = os.environ.get("FPL_DATABASE_PATH")
    return DEFAULT_DATABASE_PATH if configured is None else Path(configured)


def _release_path() -> Path | None:
    configured = os.environ.get("FPL_WEB_RELEASE_PATH")
    candidate = WEB_ROOT / "release.json" if configured is None else Path(configured)
    return candidate if candidate.is_file() else None


class SquadRequest(BaseModel):
    fpl_ids: list[int] = Field(min_length=15, max_length=15)
    bank_tenths: int = Field(default=0, ge=0)
    free_transfers: int = Field(default=1, ge=0, le=5)
    selling_prices: dict[int, int] = Field(default_factory=dict)


app = FastAPI(
    title="FPL Model Web API",
    version="0.1.0",
    description="Research-only browser boundary over the deterministic FPL decision engine.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@lru_cache(maxsize=1)
def _bootstrap() -> dict[str, object]:
    return load_web_bootstrap(_database_path(), release_path=_release_path())


@app.get("/api/health")
def health() -> dict[str, object]:
    database_path = _database_path()
    release_path = _release_path()
    return {
        "ok": release_path is not None or database_path.exists(),
        "database_path": str(database_path),
        "release_path": None if release_path is None else str(release_path),
        "mode": "compact_release" if release_path is not None else "database",
    }


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
            database_path=_database_path(),
            release_path=_release_path(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/recommend/transfers")
def transfers(request: SquadRequest, top_n: int = 8) -> dict[str, object]:
    try:
        return recommend_web_transfers(
            tuple(request.fpl_ids),
            bank_tenths=request.bank_tenths,
            free_transfers=request.free_transfers,
            selling_prices=request.selling_prices,
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
