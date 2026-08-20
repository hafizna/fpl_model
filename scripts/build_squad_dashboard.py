"""Build a local interactive squad-scenario dashboard from frozen model runs."""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

import duckdb

from fpl_model.presentation.squad_dashboard import (
    ScenarioSpec,
    build_squad_dashboard_data,
    render_squad_dashboard,
)
from fpl_model.storage import DEFAULT_DATABASE_PATH


def _scenario(value: str) -> tuple[str, Path]:
    try:
        label, path = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("scenario must use LABEL=CSV_PATH") from exc
    if not label.strip() or not path.strip():
        raise argparse.ArgumentTypeError("scenario must use LABEL=CSV_PATH")
    return label.strip(), Path(path.strip())


def _model_run(value: str) -> tuple[int, str]:
    try:
        gameweek_text, model_run_id = value.split("=", 1)
        gameweek = int(gameweek_text.removeprefix("GW").removeprefix("gw"))
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("model run must use GW=MODEL_RUN_ID") from exc
    if not 1 <= gameweek <= 38 or not model_run_id.strip():
        raise argparse.ArgumentTypeError("model run must use GW=MODEL_RUN_ID")
    return gameweek, model_run_id.strip()


def _bank(value: str) -> tuple[str, int]:
    try:
        label, amount_text = value.split("=", 1)
        amount = Decimal(amount_text)
    except (ValueError, InvalidOperation) as exc:
        raise argparse.ArgumentTypeError("bank must use LABEL=AMOUNT") from exc
    tenths = amount * 10
    if not label.strip() or amount < 0 or tenths != tenths.to_integral_value():
        raise argparse.ArgumentTypeError("bank must use LABEL=AMOUNT with at most one decimal")
    return label.strip(), int(tenths)


def _console_safe(value: str, encoding: str | None = None) -> str:
    """Keep Unicode data intact while tolerating legacy Windows consoles."""
    target_encoding = encoding or sys.stdout.encoding or "utf-8"
    return value.encode(target_encoding, errors="backslashreplace").decode(
        target_encoding
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        action="append",
        type=_scenario,
        required=True,
        metavar="LABEL=CSV_PATH",
    )
    parser.add_argument(
        "--bank",
        action="append",
        type=_bank,
        default=[],
        metavar="LABEL=AMOUNT",
        help="Optional per-scenario bank; defaults to 0.0",
    )
    parser.add_argument(
        "--model-run",
        action="append",
        type=_model_run,
        required=True,
        metavar="GW=MODEL_RUN_ID",
    )
    parser.add_argument("--source-ingestion-run", required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/squad_dashboard.html"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    bank_by_label = dict(args.bank)
    if len(bank_by_label) != len(args.bank):
        raise ValueError("each scenario bank may be specified only once")
    labels = [label for label, _ in args.scenario]
    unknown_banks = sorted(set(bank_by_label) - set(labels))
    if unknown_banks:
        raise ValueError(f"bank supplied for unknown scenarios: {unknown_banks}")
    model_run_ids = dict(args.model_run)
    if len(model_run_ids) != len(args.model_run):
        raise ValueError("each Gameweek model run may be specified only once")
    scenarios = tuple(
        ScenarioSpec(label=label, csv_path=path, bank_tenths=bank_by_label.get(label, 0))
        for label, path in args.scenario
    )
    with duckdb.connect(str(args.database), read_only=True) as connection:
        data = build_squad_dashboard_data(
            connection,
            scenarios=scenarios,
            model_run_ids=model_run_ids,
            source_ingestion_run_id=args.source_ingestion_run,
        )
    output = render_squad_dashboard(data, args.output)
    print(_console_safe(f"Wrote {output}"))
    for scenario in data["scenarios"]:
        print(
            _console_safe(
                f"{scenario['label']}: coverage={scenario['coverage']}/15 "
                f"gaps={','.join(scenario['gaps']) or 'none'}"
            )
        )


if __name__ == "__main__":
    main()
