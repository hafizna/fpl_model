"""Run the local browser recommender on http://127.0.0.1:8000."""

from __future__ import annotations

import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    uvicorn.run("api.index:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
