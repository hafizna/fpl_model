"""Run the browser recommender with a host-neutral ASGI configuration."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def runtime_address() -> tuple[str, int]:
    """Resolve a safe bind address from explicit app or conventional host variables."""

    host = os.environ.get("FPL_WEB_HOST", "127.0.0.1").strip()
    if not host:
        raise ValueError("FPL_WEB_HOST must not be empty")
    raw_port = os.environ.get("FPL_WEB_PORT", os.environ.get("PORT", "8000"))
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("FPL_WEB_PORT/PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("FPL_WEB_PORT/PORT must be between 1 and 65535")
    return host, port


def main() -> None:
    host, port = runtime_address()
    uvicorn.run("api.index:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
