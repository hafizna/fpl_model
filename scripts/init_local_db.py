"""Initialise the gitignored local DuckDB used by the FPL pipeline."""

from __future__ import annotations

import argparse
from pathlib import Path

from fpl_model.storage import DEFAULT_DATABASE_PATH, initialize_database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--path",
        type=Path,
        default=DEFAULT_DATABASE_PATH,
        help="DuckDB file path (default: %(default)s)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    info = initialize_database(args.path)
    print(f"Initialised schema v{info.schema_version}: {info.path}")
    print(f"Tables: {', '.join(info.tables)}")


if __name__ == "__main__":
    main()
