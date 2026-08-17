# Data refresh and projection architecture

## What is stable and what is not

The schema and calculation rules should be stable. The player pool is intentionally not final:
transfers, registrations, injuries, suspensions, prices, fixtures, and selection news can change
throughout preseason and every gameweek.

The pipeline therefore stores timestamped snapshots rather than overwriting one current player
table. A projection run records both its `as_of` time and target deadline. Old deadline snapshots
remain immutable so later news cannot leak into backtests.

```text
official FPL API + historical providers + reviewed availability signals
                              |
                              v
              data/processed/fpl_model.duckdb
                              |
                              v
                  Python component projections
                              |
                              v
              outputs/latest_projections.csv
                              |
                              v
                    Excel Power Query view
```

## Local files and the Benchwarmers workbooks

`MODEL.xlsx`, `SOLVER.xlsx`, and `SIMPLE MODEL.xlsx` are read-only research references, not runtime
databases and not the source of truth for weekly player state. Do not move the only copies out of
`Downloads` and do not commit them.

If a reproducible extraction needs a stable path, copy—not move—the files into
`data/raw/workbooks/`. Everything below `data/raw/` is already gitignored. Record the filename,
size, modified timestamp, and SHA-256 in the research output so the extraction can identify its
source without versioning the binary workbook.

The production pipeline must not require Excel or its chat add-in. The workbook remains a parity
oracle while the Python baseline is being reproduced.

## Local database

DuckDB is the local analytical store at `data/processed/fpl_model.duckdb`. The entire processed
directory is gitignored. Initialise it with:

```bash
python scripts/init_local_db.py
```

DBeaver is optional and should be treated as a viewer/query editor, not as the database itself.
Close DBeaver—or use a read-only connection—while the Python refresh job writes to the file.

The initial schema separates:

- `ingestion_run`: provenance and capture time for each provider pull;
- `player_snapshot`: raw FPL player/team/price/status/news state for that pull;
- `fixture_snapshot`: fixture and GW assignment as known at that time;
- `availability_signal`: timestamped injury, suspension, registration, eligibility, and selection
  evidence without prematurely collapsing conflicting sources;
- `model_run`: a projection execution bound to one ingestion snapshot and deadline;
- `player_fixture_projection`: exposure, baseline, context, uncertainty, and final xPts;
- `projection_component`: the auditable eleven-component xPts breakdown.

Provider identity joins must use stable IDs or an explicit bridge. Player-name matching is never a
runtime join strategy.

## Weekly refresh lifecycle

1. Pull FPL player and fixture state and store a new `ingestion_run`.
2. Append any news/eligibility evidence with its source and `observed_at`; never rewrite old
   evidence.
3. Validate player IDs, transfers, new players, duplicate fixtures, missing kickoff/GW assignments,
   and data freshness.
4. Resolve the evidence into availability probability, start probability, and expected-minute
   scenarios. News changes these causal inputs; it does not multiply xPts directly.
5. Run component projections for the target GW and persist a `model_run`.
6. Export `outputs/latest_projections.csv` for decision use.
7. At the deadline, mark the chosen run as the immutable deadline snapshot.
8. After matches finish, append realised outcomes for scoring and later walk-forward folds.

Suggested operating cadence:

- early week: ingest results and initial fixture/player state;
- midweek: refresh transfers, prices, fixtures, injuries, and suspensions;
- shortly before deadline: refresh again, validate freshness, and create the official run;
- after deadline: never mutate that run, even if subsequent provider fields change.

## Excel contract

Excel is a consumer of generated output, not a write target for the pipeline. A thin workbook should
load `outputs/latest_projections.csv` through Power Query. `Data -> Refresh All` updates its tables,
charts, and optimizer inputs without rewriting the original Benchwarmers formulas.

The export grain is one player-fixture row so double gameweeks remain explicit. A separate player-GW
view may aggregate fixtures for convenience. The minimum output contract is:

```text
model_run_id, as_of, deadline, gameweek, player_code, fpl_id, player_name,
team_id, fixture_id, opponent_team_id, is_home, start_probability,
substitute_appearance_probability, expected_minutes, component xPts,
baseline_xpts, context_adjustment, final_xpts, uncertainty, data_quality_flags
```

## Delivery sequence

1. Database schema and initializer.
2. FPL snapshot ingestion with raw-payload provenance and deterministic tests.
3. Availability/eligibility resolution rules with explicit source priority and manual overrides.
4. Baseline input materialisation and end-to-end projection runner.
5. CSV export and thin Excel/Power Query consumer.
6. Automated deadline snapshot, actuals ingestion, and backtesting.

Context layers and ML remain disabled until the Benchwarmers baseline has a genuine deadline-safe
benchmark.

The official FPL portion of step 1 is implemented by:

```bash
python scripts/refresh_fpl_snapshot.py --season 2026-27
```

Each run archives canonical raw JSON plus a manifest under `data/raw/fpl/`, then inserts player,
status, season-stat, team, gameweek, and fixture rows in one database transaction. The raw and
processed directories remain local and gitignored.
