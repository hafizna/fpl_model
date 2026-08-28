"""Hash one high-entropy closed-alpha access code for server configuration."""

from __future__ import annotations

import argparse
import getpass

from fpl_model.webapp.alpha_access import (
    TOKEN_HASH_ENV,
    AlphaAccessConfig,
    hash_access_token,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--label",
        required=True,
        help="Non-secret tester label used as LABEL=<sha256> in server configuration.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    token = getpass.getpass("Alpha access code (16-256 characters): ")
    entry = f"{args.label}={hash_access_token(token)}"
    AlphaAccessConfig.from_environment({TOKEN_HASH_ENV: entry})
    print(entry)


if __name__ == "__main__":
    main()
