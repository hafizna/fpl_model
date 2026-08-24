from __future__ import annotations

import argparse

from fpl_model.context.pipeline import materialize_context_features


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Materialise deadline-safe descriptive Sprint 3 context features."
    )
    parser.add_argument("--gameweek", type=int, required=True)
    parser.add_argument("--appearance-run-id")
    parser.add_argument("--database", default="data/processed/fpl_model.duckdb")
    args = parser.parse_args()
    result = materialize_context_features(
        target_gameweek=args.gameweek,
        appearance_projection_run_id=args.appearance_run_id,
        database_path=args.database,
    )
    print(f"context_run_id={result.context_run_id}")
    print(f"appearance_projection_run_id={result.appearance_projection_run_id}")
    print(f"target_gameweek={result.target_gameweek}")
    print(f"player_rows={result.player_rows}")
    print(f"fully_observed_rows={result.fully_observed_rows}")
    print(f"status={result.status}")


if __name__ == "__main__":
    main()
