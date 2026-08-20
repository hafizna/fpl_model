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
player-level xG does not reproduce the workbook team rates. The walk-forward backtest below
deliberately uses that same known-divergent aggregation anyway, because the workbook has no
historical time series to backtest against; it is never offered as a workbook replacement. After
completing
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

Generate the manual-research allowlist without introducing an uncalibrated importance score:

```bash
python scripts/export_preseason_rate_gap_triage.py --gameweek 1
```

The complete CSV is ordered by selectability, expected minutes, and then FPL ownership. This order
only prioritises data collection; it never changes xPts. Missing rate rows and linked zero-minute
provider placeholders remain distinct, auditable categories.

`scripts/export_player_rate_evidence_template.py` turns that allowlist into a targeted, prefilled
research sheet and can exclude promoted teams so manual collection stays small. The validated
importer stores completed rows in separate evidence tables with explicit comparability classes and
nullable statistics. `academy_youth` and `role_only` evidence is never silently promoted into a
senior performance rate. No evidence table is read by `baseline_pipeline.py`; league translation,
sample-size shrinkage, and position priors require a separately versioned historical backtest before
they can become production inputs.

## Walk-forward backtest of the replicated model

Unlike the GW1 preseason baseline, the walk-forward backtest scores every gameweek of a season
in-place, using only that season's own earlier gameweeks and no workbook input at all. Team
strength, player rate windows, and appearance projections are rebuilt from raw Vaastav data as of
each gameweek's deadline under a distinctly versioned, Vaastav-only policy
(`vaastav_expanding_team_strength_v1` for team strength;
`benchwarmers_replica_walk_forward_backtest_v1` for the run as a whole), then scored through the
same component functions as the GW1 baseline. Nothing is persisted to the workbook-derived
`team_strength_projection`/`player_rate_history` tables; the backtest is entirely additive and
in-memory. Run it with:

```bash
python scripts/backtest_benchwarmers.py --season 2025-26
```

The result is written to `docs/research/walk_forward_benchwarmers_2025_26.json`, including a
per-flag gap breakdown and explicit `limitations` covering the team-strength methodology
divergence, the `unused_substitute` approximation, and the re-derived (not workbook) league-average
bonus constants. Double/blank-gameweek player-fixtures and the first two gameweeks (insufficient
history) are excluded from evaluation, not specially handled.

Deadline safety is enforced by two independent conditions, not `gameweek < N` alone: every
as-of-gameweek input (player rates, appearance history, team strength, league-average bonus rates)
also requires `kickoff_time + outcome_delay <= target_deadline`. A fixture can carry an earlier
gameweek label while being postponed to kick off, and have its outcome known, only after a later
gameweek's deadline; without the timestamp condition such a fixture would silently leak into a
later prediction despite satisfying `gameweek < N`. The 2025/26 archive currently contains no such
postponement, so this fix does not change the run's metrics, but it is exercised by a dedicated
regression test that mutates a deliberately postponed fixture and asserts the earlier prediction is
unchanged.

The run also reports `matched_naive_metrics`: an expanding mean of each player's own realised
points, using the same causal availability rule, evaluated on exactly the same
`(player_code, fixture_id, gameweek)` rows the 11-component model scored. This is the only fair
naive comparison. The unrelated `docs/research/walk_forward_smoke_2025_26.json` pipeline-mechanics
smoke test evaluates a naive predictor over the *full* player population from GW2 onward, including
many zero-point unavailable/unused-substitute rows the replicated model excludes as gaps, which
mechanically understates its MAE; that number must not be quoted as a benchmark for this backtest.
`scored_coverage` (`scored_player_fixture_rows / candidate_player_fixture_rows`) and the
absolute/relative MAE and RMSE improvement over `matched_naive_metrics` are also persisted.

### Segment diagnostics

The aggregate metrics above cannot say *where* the model's bias concentrates. A read-only
diagnostic re-runs the same backtest and segments its already-scored rows without changing any
formula:

```bash
python scripts/diagnose_backtest_segments.py --season 2025-26
```

It writes `docs/research/walk_forward_benchwarmers_2025_26_segments.json`, breaking model MAE/RMSE,
`matched_naive_metrics`, and mean predicted component contributions down by position, by individual
gameweek and an early/late season split, and by population-adaptive quartile bands of expected
minutes, start probability, and predicted xPts, alongside gap rate and gap-flag counts by position.

Before any of that is written, `validation/backtest_self_check.py` loads a `--reference` JSON
(defaulting to `walk_forward_benchwarmers_2025_26.json`) and compares this run's recomputed
`import_run_id`, `evaluation_from_gw`, `evaluation_to_gw`, and aggregate `observations`/
`mean_absolute_error`/`root_mean_squared_error`/`mean_error` against it -- exact equality for the
identity/config fields, `math.isclose` with an `abs_tol` of `1e-9` for the floating-point metrics
(machine-noise allowance only, not a methodology tolerance). Any mismatch raises `ValueError`
listing every disagreeing field and the run exits before writing the segment JSON, so a silent
divergence from the production scoring path fails loudly rather than producing a misleading
breakdown. This informs later shrinkage/calibration decisions; it is not itself a scoring change.

### Paired uncertainty of the model-vs-naive advantage

A single MAE/RMSE improvement number cannot say whether the model's advantage over
`matched_naive_metrics` is a stable signal or noise from one season's 36 gameweeks. A read-only
measurement quantifies this with a paired, gameweek-cluster bootstrap:

```bash
python scripts/analyze_backtest_uncertainty.py --season 2025-26
```

Rows within one gameweek are not independent -- every row scored at a gameweek's deadline shares
that gameweek's causally re-derived team-strength/rate/appearance inputs -- so `validation/
paired_uncertainty.py` resamples whole gameweeks (10,000 resamples, fixed seed `42` by default,
2.5th/97.5th percentile CI), not individual rows, keeping every row inside a resampled gameweek
together as one block. A fixture-clustered bootstrap is reported alongside as a labelled
sensitivity check, never as the primary result. It writes
`docs/research/walk_forward_benchwarmers_2025_26_uncertainty.json`, self-checking both the
aggregate backtest metrics (reusing `verify_self_check`) and its own recomputed MAE/RMSE point
estimates against the reference's `absolute_mae_improvement`/`absolute_rmse_improvement` before
writing anything. Its `limitations` note that a CI excluding zero supports stability within this
one-season sample, not universal superiority. This is measurement only; it does not itself change
any formula, calibration, or shrinkage.

### Walk-forward xPts calibration

A separate read-only measurement asks whether a post-hoc calibration slope
(`actual_points ~ predicted_xpts`) would help, if it were fit strictly on prior gameweeks and
applied only to the following gameweek's unseen rows:

```bash
python scripts/assess_backtest_calibration.py --season 2025-26
```

`validation/walk_forward_calibration.py` fits a closed-form OLS slope/intercept at each eligible
evaluation gameweek `G` using only rows from gameweeks strictly before `G` (the same no-lookahead
boundary every other backtest input already enforces), separately for the overall row set and for
a high-predicted-xPts band whose threshold is itself recomputed from that same prior-only pool at
each step. Two distinct slope numbers are reported: the **trajectory** (each step's own
fitted-on-prior-data slope, showing whether the fit stabilises across the season) and the
**pooled walk-forward slope** (a second OLS fit directly on the concatenation of every eligible
step's own out-of-sample evaluation rows, answering how calibrated the walk-forward predictions
actually were in aggregate). CIs for the pooled slope and for calibrated-vs-raw/calibrated-vs-naive
MAE improvement reuse `validation/paired_uncertainty.py`'s gameweek-cluster bootstrap via the
shared `block_bootstrap_statistic` primitive. It writes
`docs/research/walk_forward_benchwarmers_2025_26_calibration.json`, self-checking the aggregate
backtest metrics before any calibration work begins. This is measurement only: no calibration is
applied to any production projection or scoring formula.

### Walk-forward appearance-model calibration

The xPts calibration's high-predicted-xPts band is materially overconfident, but
`start_probability`/`expected_minutes` and the per-90 rate components built on top of them
(goals/assists/bonus/etc.) are highly correlated there, so that finding alone cannot say which
side of the model is responsible. A separate read-only measurement isolates the appearance model
itself, using the identical walk-forward/no-lookahead methodology:

```bash
python scripts/assess_appearance_calibration.py --season 2025-26
```

`validation/appearance_calibration.py` fits two independent causal OLS calibrations at each
eligible evaluation gameweek: `actual_started ~ start_probability` and `actual_minutes ~
expected_minutes`, both using only strictly-prior gameweeks, applied only to that gameweek's own
unseen evaluation rows. Realised `actual_started`/`actual_minutes` are read directly from that
gameweek's own `merged_gw.csv` row, the same source `actual_points` is read from. As with the xPts
calibration, a pooled diagnostic slope is reported separately from the causal calibrated
prediction, and is never used to produce one (that would be in-sample). Because
`start_probability` is a bounded 0/1 target, MAE and MSE can disagree in sign -- OLS minimises
squared error, so a slope below 1.0 can reduce MSE while increasing MAE -- so both are reported,
with MSE treated as the primary "is this a real miscalibration" signal for that target. It writes
`docs/research/walk_forward_benchwarmers_2025_26_appearance_calibration.json`, self-checking the
aggregate backtest metrics before any calibration work begins. This is measurement only: no
calibration is applied to any production projection or scoring formula.

### Appearance-model bias segment diagnostic

The appearance calibration above found both targets' pooled slope below 1.0 in aggregate; it
cannot say *where* that average over/under-prediction bias concentrates (note: mean(predicted -
actual) measures bias, not "confidence" -- overconfidence is the correct word for the pooled
slope above, not for this segmented mean bias). A further read-only diagnostic segments the same
causal predictions by fixed `start_probability`/`expected_minutes` bands, position, and a
fixed-boundary gameweek phase, over four cohorts -- `appearance_eligible` (primary),
`xpts_scored_aligned` (sensitivity, spans every evaluated gameweek), `xpts_high_band_aligned`
(sensitivity: the subset of `xpts_scored_aligned` whose keys were also members of the xPts
calibration's own out-of-sample, prior-only 75th-percentile high-band), and
`xpts_same_window_aligned` (sensitivity: the correct comparator for high-band claims -- the
subset of `xpts_scored_aligned` sharing the SAME eligible-gameweek window `xpts_high_band_aligned`
was drawn from, read directly from `walk_forward_calibration`'s own `overall_evaluation_rows`,
never inferred from the high band's own min/max gameweek). Both new cohorts are derived from one
`walk_forward_calibration` call so membership can never drift from that committed result or from
each other's window:

```bash
python scripts/diagnose_appearance_segments.py --season 2025-26
```

`validation/appearance_segments.py` deliberately never fits a per-segment regression slope --
several required segments (e.g. the top start_probability/expected_minutes band) have too little
predictor variance to support one -- and instead reports mean bias (predicted - actual), `bias_sum`
(`mean_bias * rows`, additive across a partition -- the mathematically real "contribution to
aggregate bias" quantity, unlike `abs(mean_bias)` alone, which cannot distinguish a segment
contributing to the aggregate from one offsetting it), a gameweek-cluster bootstrap CI, and Brier
score/observed start rate or MSE/MAE, flagging `insufficient_variation` rather than fabricating an
unstable slope. Segment bands are fixed absolute thresholds, not population-adaptive quantiles
(unlike `diagnose_backtest_segments.py`'s unrelated xPts-band quartiles), so a band's meaning is
stable across cohorts and reruns; the gameweek-phase boundaries are a new, fixed diagnostic
convention -- canonical season thirds GW1-13/14-26/27-38 -- documented in that module (not
`docs/DATA_MODEL.md`'s short-form rate windows, which are a previous-season rate-history boundary
for an unrelated purpose). "Stable"/"excludes zero" wording on any bootstrap CI is withheld below
a minimum distinct-gameweek-cluster count regardless of what the CI itself shows, since a
percentile bootstrap from very few clusters can exclude zero by chance. The gameweek-cluster
bootstrap itself is computed from precomputed per-gameweek sufficient statistics rather than
rescanning every row per resample -- a runtime optimisation only, reproducing
`block_bootstrap_statistic`'s numbers to floating-point tolerance.

Any "concentrated"/"larger" claim comparing two cohorts (a top band vs the primary cohort's
overall bias; the xPts high band vs its same-window comparator) is licensed ONLY by
`paired_contrast_bootstrap`'s own PAIRED gameweek-cluster contrast CI
(`focus_bias - comparator_bias`, one shared cluster-label draw per replicate applied to both
sides, never two independently-bootstrapped CIs subtracted) lying entirely above zero -- a single
side's own CI excluding zero is necessary but not sufficient, since both sides can be individually
significant and same-signed while their difference's own sampling uncertainty still crosses zero.
`correction_recommendation` requires all four required contrasts (both top bands vs overall; both
xPts-high-vs-same-window targets) to show this before suggesting high-end-only shrinkage is worth
testing; any other combination reports mixed/inconclusive evidence target by target instead. It
writes `docs/research/walk_forward_benchwarmers_2025_26_appearance_segments.json`, self-checking
the aggregate backtest metrics before any segment work begins. This is measurement only: it does
not prove which component causes total xPts error in a biased segment (the per-90 rate components
are highly correlated with start_probability/expected_minutes in exactly the bands found most
biased), has not compared a global calibration policy against a high-end-only policy out of
sample, and no calibration, shrinkage, or formula change is applied to production.

### Causal appearance calibration policy backtest

The segment diagnostic above is read-only: it never recomputes `predicted_xpts` under any
calibration, so it cannot say whether applying one would actually help out of sample. A further
script materializes and scores three POLICIES head-to-head from one shared walk-forward pass:

```bash
python scripts/backtest_appearance_calibration_policies.py --season 2025-26
```

- `raw`: the existing, unmodified model (the control) -- its own aggregate metrics are checked
  against `--reference`, AND its individual rows are compared field-by-field (EVERY
  `BacktestObservation` field including `season`; every gap's `team`/`position`/`flags`, not flags
  alone; duplicate keys on either side detected explicitly before any dict-keyed comparison) against
  `benchwarmers_backtest.py`'s own canonical materializer output (`raw_row_level_parity`) before any
  comparison is trusted. The aggregate self-check alone cannot rule out two different row-level
  results sharing the same aggregate mean/sum-of-squares; the row-level check is the evidence that
  actually establishes `validation/appearance_policy_backtest.py`'s duplicated walk-forward loop has
  exact field-level parity with the committed materializer.
- `global`: the causal walk-forward `start_probability` OLS calibration (refit every gameweek on
  strictly-prior, DEADLINE-SAFE gameweeks only -- see below) applied to every row.
- `high_end_shrinkage`: that SAME per-gameweek fit applied only to rows whose RAW
  `start_probability` is at or above 0.8 -- the fixed `[.8,1]` band edge
  `validation/appearance_segments.py` already uses. Every other row keeps its raw prediction.

**What "causal" does and does not mean here.** Every fit is deadline-safe in the same sense the
primary xPts backtest already is: a calibration row is eligible for gameweek `G`'s fit only when
BOTH `row.gameweek < G` AND `row.outcome_available_at <= G`'s deadline (`kickoff_time +
outcome_delay`) -- `row.gameweek < G` alone is not sufficient, since a postponed fixture can carry
an earlier gameweek label while its outcome becomes known only after a later gameweek's deadline.
This closes the same hazard `benchwarmers_backtest.py`'s own deadline-safety fix already closed for
the primary backtest, applied a second time to the calibration rows specifically. That guarantees no
future OUTCOME ever enters an earlier fit -- but it does NOT mean the segment diagnostic's findings
"cannot leak" into this evaluation in the broader sense: the DECISION to test `high_end_shrinkage` as
a candidate policy at all was made after inspecting `diagnose_appearance_segments.py`'s own 2025-26
results, then evaluated on that SAME season again. This is causal walk-forward EXECUTION of an
EXPLORATORY, SAME-SEASON policy specification, not an independent confirmatory backtest -- the
script's own bootstrap CIs quantify sampling uncertainty in the paired comparison only, not this
policy-selection uncertainty. Any verdict is better-supported WITHIN this exploratory comparison; an
independent season or a prospectively frozen 2026-27 evaluation is required before production
adoption.

A separate `expected_minutes` OLS calibration is out of scope: tracing every
`weight_*`/`project_benchwarmers_*` function, only `start_probability` (and the fields dependent on
it) is ever read by the scoring chain, so fitting/applying an independent `expected_minutes`
calibration could not move any policy's score -- a notable finding surfaced explicitly, not a silent
omission. None of the three tested policies is a pure `start_probability` substitution, though:
each applies an OLS transform to `start_probability` and then proportionally rescales every DEPENDENT
field (`substitute_appearance_probability`, `sixty_minute_probability`, `appearance_xpts`,
`sixty_minute_xpts`, `total_xpts`, and `expected_minutes`) so the returned projection stays
genuinely self-consistent; `rescale_appearance_projection` scales every dependent field by
`calibrated_start_probability / raw_start_probability` (clamped to each field's own natural ceiling)
and recomputes `expected_minutes` itself from the calibrated/rescaled start and substitute
probabilities (`calibrated_start_probability * mean_minutes_per_start + rescaled_substitute_probability
* mean_minutes_per_substitute`, the same weighted-sum formula
`model.appearance.blend_conditional_appearance` already uses) -- not a new scoring rule, and this
recomputation still cannot move any score, since `expected_minutes` remains causally inert. The
performance results this backtest produces evaluate that COMPLETE reconstruction rule, not calibrated
`start_probability` in isolation. The rescaled projection is then threaded through the UNCHANGED
`weight_*`/`compose_baseline_projection` chain -- `baseline_pipeline.py` and every
`project_benchwarmers_*`/`weight_*` component formula are never modified.

Policy comparisons (`global_vs_raw`, `high_end_shrinkage_vs_raw`, `high_end_shrinkage_vs_global`)
reuse `validation/paired_uncertainty.py`'s `build_paired_rows`/`cluster_bootstrap` -- the SAME
gameweek-cluster percentile bootstrap the model-vs-matched-naive comparator already uses, valid
here because all three policies score identical candidate rows by construction (verified before
any comparison is computed). A verdict of `"improves"` requires both MAE and RMSE improvement CIs
entirely above zero (mirroring `interpret_paired_verdict`'s own direction-aware three-state
classification); the script's own `recommendation` additionally checks for a one-sided reliable
loss on either metric even when the two-metric verdict is `"mixed_or_inconclusive"`, so a
high-end-only policy that is reliably worse than global on MAE alone is not silently treated as a
tie. It writes
`docs/research/walk_forward_benchwarmers_2025_26_appearance_policy_backtest.json`. This is
measurement only, and an EXPLORATORY same-season comparison: an out-of-sample MAE/RMSE advantage
here is evidence for, but does not by itself constitute, a decision to change
`baseline_pipeline.py`'s production formula -- an independent season or prospectively frozen
evaluation is required first.

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

The first implemented decision-layer boundary is the immutable squad tracker. A manager CSV keeps
only private state (owned FPL IDs, purchase/selling prices, order, captaincy), while a pinned
official `ingestion_run` supplies identity, team, position, current price, and deadline. The
validated import writes `manager_entry`, `squad_snapshot`, `squad_snapshot_player`, and
`squad_chip_state`; DBeaver or another SQL client may inspect them read-only, but operational writes
go through `scripts/import_squad_snapshot.py` so lineage and FPL-constraint checks cannot be
bypassed accidentally.

The next implemented boundary is deliberately solver-free. `scripts/recommend_lineup.py`
exhaustively enumerates legal XIs from the fixed squad. `scripts/recommend_transfers.py` compares
that no-transfer baseline with every affordable, legal, same-position one-player swap, rerunning
the XI and captain search after each swap. It uses exact selling values and bank, applies an
immediate hit when no free transfer exists, aggregates DGWs, excludes untransactable or unprojected
targets with explicit counts, and preserves all quality flags. This is a transparent single-GW
benchmark, not yet the multi-GW planner described above.

The rolling three-GW engine is now also implemented, but is not yet operationally validated. It
loads exactly three consecutive `model_run` records completed by the first deadline and sharing the same official source snapshot,
model version, and frozen pre-first-deadline `as_of`. It searches roll/single-transfer paths,
advances free-transfer and bank state, applies hits, and optimizes lineup/captaincy independently in
each GW. Candidate-per-position pruning and a bounded beam keep the search tractable; therefore the
recommended plan is the best retained path, not a certified global optimum.

This engine implements a planned GW N..N+2 decision cadence. The plan is normally reviewed after
the third GW, while a confirmed injury, suspension/red card, real-world transfer/registration
change, or material evidence-backed role change may trigger an earlier replan. The existing frozen
appearance-calibration policy is upstream and is deliberately untouched. A planner protocol, if
needed, will be frozen separately after planner backtesting.

The preseason future-GW boundary is now implemented separately from the canonical GW1 materializer.
`scripts/project_frozen_horizon.py` reuses a completed baseline anchor, queries GW+1/GW+2 fixtures
and deadlines from the same official ingestion, and reruns all eleven components against each new
opponent and venue. Appearance, player rates, team strength, and minutes inputs remain frozen to
the anchor `as_of`; every future projection row carries an explicit frozen-input flag. This is not
a substitute for the future in-season horizon refresh, which still needs causal current-season
inputs and an availability-decay/return policy.

The public-data preseason decision boundary is also implemented separately from manager tracking.
`scripts/optimize_initial_squad.py` consumes the same three compatible model runs and requires no
private squad input. It prunes candidates through horizon-xPts, value, and cheap-enabler lenses,
constructs budget/position/club-legal squads with a bounded beam, and exactly re-optimizes lineup
and captaincy for each retained squad in each GW. This enables general preseason selection while a
manager snapshot is unavailable, without pretending the rolling one-transfer engine supports an
unlimited-transfer state. The search remains explicitly approximate.

Implementation order remains deliberately simple and auditable:

1. select an initial preseason squad from public data (implemented as a bounded beam search);
2. validate and version the exact 15-player manager state;
3. enumerate a legal starting XI, bench order, captain, and vice-captain from that fixed squad;
4. enumerate no-transfer and single-transfer alternatives using exact sale value and bank;
5. add rolling three-Gameweek scoring, FT state, and uncertainty (implemented as bounded beam
   search; awaiting future-GW projections and backtesting);
6. add multi-transfer/chip state and introduce a solver only when the expanded search requires it.
