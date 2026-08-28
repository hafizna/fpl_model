"""Validate lineup/rating latency against one materialized compact release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fpl_model.validation.web_latency import validate_web_latency


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--fpl-id", type=int, action="append", dest="fpl_ids", required=True)
    parser.add_argument("--bank-tenths", type=int, default=0)
    parser.add_argument("--cold-limit-ms", type=float, default=3_000.0)
    parser.add_argument("--cached-limit-ms", type=float, default=1_000.0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = validate_web_latency(
        tuple(args.fpl_ids),
        release_path=args.release,
        bank_tenths=args.bank_tenths,
        cold_limit_ms=args.cold_limit_ms,
        cached_limit_ms=args.cached_limit_ms,
    )
    output = json.dumps(result.report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    raise SystemExit(0 if result.passes else 2)


if __name__ == "__main__":
    main()
