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
python scripts/import_player_identity_bridge.py \
  --vaastav-players-csv data/raw/vaastav/2025-26/<pinned-revision>/players_raw.csv \
  --source-ingestion-run-id fpl_... \
  --target-season 2026-27 \
  --vaastav-season 2025-26 \
  --source-revision <pinned-revision>
python scripts/resolve_availability.py --gameweek 2
python scripts/refresh_fpl_event_live.py \
  --gameweek 1 \
  --source-ingestion-run fpl_... \
  --allow-analytically-complete
# Only when the event-live command prints status=completed (officially final):
python scripts/add_penalty_review.py \
  --live-run-id fpl_live_... \
  --csv data/raw/reviews/gw1_penalties.csv \
  --observed-at 2026-08-25T04:00:00+07:00 \
  --source-reference "reviewed match report" \
  --rationale "complete GW1 penalty ledger"
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

The production-shaped manual sequence above is also available as one fail-closed command:

```bash
python scripts/materialize_release.py --help
```

For scheduled operations, use the platform-neutral wrapper:

```bash
python scripts/run_deadline_refresh.py --help
```

It adds an exclusive lock, optional pre-run DuckDB backup, atomic compact-release publication,
machine-readable status/exit codes, and optional webhook alerting around the same materialization
function. See `docs/DEADLINE_REFRESH_WORKER.md`. It does not relax any finality or freshness gate.

It additionally attaches the named calibration and uncertainty artifacts and runs manifest,
freshness, approval, and health validation over the resulting three model runs. The command exits
non-zero when manifest or freshness validation fails; a healthy release can still remain
`shadow` until the artifacts pass confirmatory evaluation and are approved.

By default, `refresh_fpl_event_live.py` fails closed unless the source snapshot says the prior
Gameweek is both `finished` and `data_checked`. For the narrower appearance/context refresh,
`--allow-analytically-complete` admits a non-final event only when every fixture assigned to that
Gameweek is either `finished` or `finished_provisional` in the same immutable official snapshot.
The latter is FPL's final-whistle state before its later data-check pass. The stored run remains `provisional`,
and every downstream appearance/context row carries
`OFFICIAL_EVENT_ANALYTICALLY_COMPLETE_NOT_FINAL`. Re-ingest and rebuild after FPL sets
`data_checked`; immutable run IDs preserve both versions.

`project_inseason_appearance.py` requires an analytically complete event-live run for every earlier
Gameweek. For GW3, for example, both GW1 and GW2 must exist. The broader `--allow-provisional`
option has no fixture-completion gate and remains research-only; do not use it for a production
recommendation.

The penalty review is a separate final-only boundary and cannot be attached to an analytically
complete provisional run. Official total xG is retained, but npxG is withheld until FPL finalises
the event and the complete penalty ledger has been reviewed. The current early-season baseline
still uses frozen previous-season player rates, so this decomposition is stored for the future
attacking-rate refresh rather than applied as an immediate multiplier.

The refreshed appearance projection shrinks current-season starts, cameos, and minutes toward the
reviewed previous-season appearance history. With the default five effective prior fixtures, one
completed current-season fixture receives weight `1 / (1 + 5)`. This avoids overreacting to one match
while still allowing a new/current-only player to acquire an evidence-based role.

## Context contract

`reviewed_context_annotation` stores immutable, sourced annotations for:

- team manager regimes;
- player tournament/preseason readiness;
- continuous tactical-role fingerprints and nominal-position changes.

`player_context_feature` combines those annotations with official playing-time history to
store rest days and minutes/matches over seven- and fourteen-day windows. Analytically complete
non-final evidence retains the explicit provisional quality flag. The official endpoint
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

Before every prior fixture is finished, the last valid recommendation is the frozen earlier
horizon. Once all fixtures are finished, the appearance/context refresh may produce a flagged
analytical anchor without waiting for mini-league processing. After FPL marks the event final and
data-checked, rerun the full sequence to replace that provisional evidence with a final immutable
run. Frozen previous-season player rates and team strength remain visible as quality flags; they
are not presented as current-season estimates.
