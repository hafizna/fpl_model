"""Compare two compact web releases and optionally rescore one owned squad."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fpl_model.validation.release_drift import compare_web_releases


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--fpl-id", type=int, action="append", dest="fpl_ids")
    parser.add_argument("--bank-tenths", type=int, default=0)
    parser.add_argument("--free-transfers", type=int, default=1)
    parser.add_argument("--xpts-threshold", type=float, default=0.25)
    parser.add_argument("--appearance-threshold", type=float, default=0.05)
    parser.add_argument("--skip-transfers", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = compare_web_releases(
        before_path=args.before,
        after_path=args.after,
        owned_fpl_ids=None if args.fpl_ids is None else tuple(args.fpl_ids),
        bank_tenths=args.bank_tenths,
        free_transfers=args.free_transfers,
        xpts_threshold=args.xpts_threshold,
        appearance_threshold=args.appearance_threshold,
        include_transfer_scan=not args.skip_transfers,
    )
    output = json.dumps(result.report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")


if __name__ == "__main__":
    main()
