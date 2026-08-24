# Deadline-safe in-season refresh

Sprint 3 refreshes appearance and descriptive context before each deadline while preserving the
previous-season rate and team-strength priors explicitly. It never treats a partially completed
Gameweek as final evidence and it never turns a context annotation into an arbitrary xPts
multiplier.

## GW2+ run order

Run these commands from the repository root. Keep the IDs printed by every command; they are the
immutable lineage of the recommendation.

```bash
python scripts/refresh_fpl_snapshot.py --season 2026-27
python scripts/resolve_availability.py --gameweek 2
python scripts/refresh_fpl_event_live.py \
  --gameweek 1 \
  --source-ingestion-run fpl_...
python scripts/project_inseason_appearance.py \
  --gameweek 2 \
  --current-season 2026-27 \
  --previous-season 2025-26
python scripts/import_team_strength.py \
  --csv data/raw/workbooks/benchwarmers_team_strength_2026_27.csv \
  --target-season 2026-27 \
  --previous-season 2025-26 \
  --source-label "MODEL.xlsx reviewed team-strength export" \
  --gameweek 2 \
  --source-ingestion-run fpl_...
python scripts/materialize_context_features.py \
  --gameweek 2 \
  --appearance-run-id appearance_...
python scripts/project_inseason_baseline.py \
  --gameweek 2 \
  --appearance-run appearance_... \
  --team-strength-run team_strength_run_... \
  --context-run context_run_...
python scripts/project_frozen_horizon.py --anchor-model-run-id baseline_...
```

`refresh_fpl_event_live.py` fails closed unless the source snapshot says the prior Gameweek is both
`finished` and `data_checked`. `project_inseason_appearance.py` also requires one completed final
event-live run for every earlier Gameweek. For GW3, for example, both GW1 and GW2 must exist. Do not
use `--allow-provisional` for a production recommendation.

The refreshed appearance projection shrinks current-season starts, cameos, and minutes toward the
reviewed previous-season appearance history. With the default five effective prior fixtures, one
final current-season fixture receives weight `1 / (1 + 5)`. This avoids overreacting to one match
while still allowing a new/current-only player to acquire an evidence-based role.

## Context contract

`reviewed_context_annotation` stores immutable, sourced annotations for:

- team manager regimes;
- player tournament/preseason readiness;
- continuous tactical-role fingerprints and nominal-position changes.

`player_context_feature` combines those annotations with final official playing-time history to
store rest days and minutes/matches over seven- and fourteen-day windows. The official endpoint
does not cover European or other non-PL minutes, so every row carries
`NON_PL_WORKLOAD_NOT_INGESTED` until that evidence is supplied. Aggregated DGW minutes are not
guessed onto individual fixtures.

Context rows are diagnostic-only and carry `CONTEXT_FEATURES_DIAGNOSTIC_ONLY`. The baseline links
the exact context run and exposes its quality flags, but `context_adjustment` remains zero. A layer
may change production inputs only after its otherwise-identical ablation clears the paired,
gameweek-clustered MAE and RMSE gate in `validation/context_ablation.py`.

Annotations can be added with `scripts/add_context_annotation.py`; run `--help` for the typed JSON
payload contract. Store the source URL/reference and a concise review rationale with each row.

## Operational interpretation

Before the prior Gameweek is final, the last valid recommendation is the frozen earlier horizon.
Once the final event data is checked, rerun the full sequence and generate a new GW anchor. Frozen
previous-season player rates and team strength remain visible as quality flags; they are not
presented as current-season estimates.
