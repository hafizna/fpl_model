from __future__ import annotations

import argparse

from fpl_model.validation.projection_uncertainty import apply_uncertainty_artifact


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Attach shadow/active uncertainty intervals to one model run."
    )
    parser.add_argument("--model-run-id", required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--database", default="data/processed/fpl_model.duckdb")
    args = parser.parse_args()
    result = apply_uncertainty_artifact(
        model_run_id=args.model_run_id,
        artifact_id=args.artifact_id,
        database_path=args.database,
    )
    print(f"model_run_id={result.model_run_id}")
    print(f"artifact_id={result.artifact_id}")
    print(f"rows={result.player_fixture_rows} mode={result.application_mode}")


if __name__ == "__main__":
    main()
