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
- `availability_override`: explicitly reviewed, sourced, gameweek-specific corrections;
- `availability_resolution_run` and `player_availability_resolution`: immutable output of the
  deadline-safe availability policy, including unresolved values and data-quality flags;
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

The official availability policy is materialised separately:

```bash
python scripts/resolve_availability.py --gameweek 1
```

It uses the latest FPL snapshot captured no later than the target deadline. Explicit official
chance values are retained; an available player with a blank chance resolves to 1.0; official
suspension/unavailable/removed states resolve to an eligibility block. Other missing probabilities
remain `NULL` and block downstream projection rather than being guessed. A reviewed override can
replace those fields only when its observation timestamp is no later than the selected snapshot.
Free-text news is stored for review but is not automatically converted into a probability.

Reviewed evidence can be appended without editing database rows manually:

```bash
python scripts/add_availability_override.py \
  --player-code 123456 --gameweek 1 \
  --observed-at 2026-08-21T10:00:00+07:00 \
  --probability 0.75 --eligible \
  --source club_press_conference \
  --rationale "Manager confirmed the player trained; late decision"
```

The command rejects evidence observed after the deadline. If the evidence is newer than the latest
FPL snapshot—or that snapshot has already been resolved—it asks for another FPL refresh so the old
resolution remains immutable.

The workbook appearance-history boundary is documented in
`docs/research/CLAUDE_APPEARANCE_HISTORY_EXPORT_PROMPT.md`. Once that read-only export exists, the
validated GW1 handoff is:

```bash
python scripts/import_appearance_history.py \
  --csv data/raw/workbooks/benchwarmers_appearance_history_2025_26.csv \
  --season 2025-26 \
  --source-label "MODEL.xlsx resolved previous-season appearance fields"
python scripts/project_preseason_appearance.py --gameweek 1 --previous-season 2025-26
```

The importer rejects duplicate codes, missing values, fractional counts, invalid minute ranges, and
players with positive starts/sub appearances but zero corresponding mean minutes. The projection
table retains every current FPL player; unmatched history produces null projection fields plus a
`NO_WORKBOOK_APPEARANCE_HISTORY` flag.

For a new signing or a reviewed role change, append a conditional appearance scenario rather than
turning missing history into zero:

```bash
python scripts/add_appearance_scenario_override.py \
  --player-code 123456 --gameweek 1 \
  --observed-at 2026-08-21T10:00:00+07:00 \
  --start-if-available 0.65 --sub-if-available 0.25 \
  --sixty-given-start 0.85 \
  --minutes-per-start 76 --minutes-per-substitute 20 \
  --source reviewed_team_news \
  --rationale "New signing expected to compete for a starting role"
```

These inputs describe playing time conditional on availability. They require timestamped evidence
and never modify an existing projection run. When the command requests a refresh, capture a new FPL
snapshot, resolve availability again, and materialise a new appearance projection.

Previous-season player rate inputs can be archived and materialised independently:

```bash
python scripts/import_vaastav_player_history.py --season 2025-26
```

The command selects the newest revision that actually changed `gws/merged_gw.csv`, fetches both
source files from that exact commit, archives canonical CSVs and a manifest under
`data/raw/vaastav/`, validates season totals, and transactionally stores player-fixture facts plus
38-GW/6-GW/10-GW rate windows. It never reads current preseason FPL carry-over totals as if they
were observations from the new season. The reproducible 2025/26 audit is in
`docs/research/vaastav_preseason_rate_import_2025_26.json`.

Team-strength input is a separate reviewed workbook boundary because aggregating Vaastav's
player-level xG does not reproduce the workbook team rates. After completing
`docs/research/CLAUDE_TEAM_STRENGTH_EXPORT_PROMPT.md`, import and materialise it with:

```bash
python scripts/import_team_strength.py \
  --csv data/raw/workbooks/benchwarmers_team_strength_2026_27.csv \
  --target-season 2026-27 --previous-season 2025-26 \
  --source-label "MODEL.xlsx TABLES resolved preseason team windows"
```

The importer requires all 20 current FPL abbreviations and exactly three explicit promoted-team
priors. The official FPL snapshot supplies current team IDs and the GW1 deadline; it does not
silently override the reviewed xG/xGC rates.

With all three baseline inputs available, compose and persist GW1 projections with:

```bash
python scripts/project_preseason_baseline.py --gameweek 1
```

For an exact reproduction, also pass `--appearance-run`, `--player-rate-run`, and
`--team-strength-run`. The selected IDs, policy version, FPL snapshot, as-of time, and deadline are
stored with the run. `completed_with_gaps` is a valid research result: it means linked players were
projected while missing inputs were itemised in `baseline_projection_gap`. It must not be treated
as full player-pool coverage by the later CSV export or optimiser.

## Decision layer: squad planner and transfer recommender

The eventual user-facing feature sits downstream of calibrated player-fixture projections. Its
inputs are the manager's current 15-player squad, purchase and sale values, bank, free transfers,
chips, per-gameweek component xPts, availability, expected minutes, uncertainty, data-quality
flags, FPL constraints, hit cost, planning horizon, and risk preference.

The decision layer will produce:

- a starting XI, bench order, captain, and vice-captain;
- transfer suggestions, including a no-transfer option;
- multi-gameweek net gain after hit costs and budget constraints;
- safer alternatives when the best mean-xPts move has high uncertainty;
- an explanation of the components and assumptions driving each recommendation.

Missing projections remain explicit gaps and cannot be interpreted as cheap zero-value players.
