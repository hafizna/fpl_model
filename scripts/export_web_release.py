"""Export a validated three-Gameweek release for stateless web deployment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fpl_model.storage import DEFAULT_DATABASE_PATH
from fpl_model.webapp.release_export import build_web_release


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-run-id", action="append", dest="model_run_ids")
    parser.add_argument("--require-production", action="store_true")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = build_web_release(
        model_run_ids=None if args.model_run_ids is None else tuple(args.model_run_ids),
        require_production=args.require_production,
        database_path=args.database,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result.payload, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "release_id": result.release_id,
                "health": result.health,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
