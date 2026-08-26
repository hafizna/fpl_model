"""FastAPI adapter for the browser recommender and Vercel Python runtime."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.webapp.service import (
    load_web_bootstrap,
    recommend_web_lineups,
    recommend_web_transfers,
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
