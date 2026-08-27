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

`--effective-until` is optional: omit it and the override is stored with `effective_until` set to
the target Gameweek's own deadline, so it can never silently outlive the Gameweek it was reviewed
for. Pass an earlier timestamp explicitly to expire the override sooner (for example, "reassess
after Thursday's training report"). A value later than the deadline is rejected outright rather than
silently clamped, since that would hide a reviewer's mistaken belief that the override applies
further out. The same rule applies to `add_appearance_scenario_override.py` below. Both commands
print the effective expiry that was actually stored.

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

A reviewer who does not want to hand-pick all five fields can start from a named preset instead:

```bash
python scripts/add_appearance_scenario_override.py \
  --player-code 123456 --gameweek 1 \
  --observed-at 2026-08-21T10:00:00+07:00 \
  --preset rotation_risk \
  --source reviewed_team_news \
  --rationale "Returning from injury, expected to be managed carefully"
```

`context/appearance_scenario_presets.py` defines three presets -- `likely_starter`, `rotation_risk`,
`likely_bench` -- whose probability bands deliberately reuse `role_state.py`'s own
`ROTATION_THRESHOLD`/`LIKELY_STARTER_THRESHOLD` constants and `model/appearance.py`'s own default
minutes, so a preset's name means the same thing a manager already sees in a player's role state. Any
of the individual `--start-if-available`/`--sub-if-available`/`--sixty-given-start`/
`--minutes-per-start`/`--minutes-per-substitute` flags can still be combined with `--preset` to
override one field of the chosen preset -- the result passes through the same
`ConditionalAppearanceScenario` validation a fully hand-built scenario would.

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

## Sprint 5: release manifest

Every upstream artifact above (`ingestion_run`, the player identity bridge,
availability resolution, appearance projection, player rates, team strength,
context features, shadow calibration, shadow uncertainty) is versioned and linked
from `model_run`/`baseline_projection_run`, but nothing yet asserted that a chosen
set of model runs actually cohere as one releasable horizon, or gave a downstream
consumer one document to point at instead of re-deriving that lineage itself. The
first Sprint 5 deliverable closes that gap without deciding freshness, coverage, or
calibration on its own:

```bash
python scripts/build_release_manifest.py \
  --model-run baseline_... \
  --model-run baseline_... \
  --model-run baseline_...
```

Pass one `--model-run` per Gameweek in the intended release (normally the anchor GW
plus its frozen GW+1/GW+2 horizon from `project_frozen_horizon.py`), ordered by
ascending target Gameweek. `build_release_manifest` in
`validation/release_manifest.py` never selects "the latest" run itself -- a release
is a deliberate, reproducible act naming its exact inputs. For each run it walks the
full lineage graph (ingestion, identity bridge, availability resolution, appearance
projection, player rates, team strength, context features, shadow calibration,
shadow uncertainty) and checks that every run in the set:

- has `status` of `completed` or `completed_with_gaps`;
- targets a distinct Gameweek, given in ascending order;
- shares one `source_ingestion_run_id` (the same official FPL snapshot) and one
  frozen `as_of` across the whole horizon -- the same coherence rule
  `plan_three_gameweeks.py` and `optimize_initial_squad.py` already require of their
  own three-run inputs;
- has a linked `baseline_projection_run`, identity bridge, appearance lineage,
  player-rate run, and team-strength run for every included Gameweek.

Any violation is collected into `linkage.problems` and the manifest fails closed:
`linkage.passes` is `false` and the CLI exits non-zero. The manifest also reports,
without failing on it, which Gameweeks are missing shadow calibration, shadow
uncertainty, or context lineage, and separately resolves every in-season
appearance run's `live_run_ids` into full `fpl_event_live_run` rows, flagging any
that are not both `event_finished` and `data_checked` -- i.e. built from an
`OFFICIAL_EVENT_ANALYTICALLY_COMPLETE_NOT_FINAL` run per
`docs/INSEASON_REFRESH.md`. This is visibility for the separate freshness/coverage/
calibration/uncertainty gates the rest of Sprint 5 still needs to add, not a
pass/fail verdict those gates own. The output is one content-hashed
`manifest_id` (`release_manifest_<sha256 prefix>`), so the same run set always
reproduces the same manifest identity.

## Sprint 5: freshness, fixture-completion, and FPL-finality checks

The manifest above proves lineage coherence; it does not ask whether that evidence
is still current or whether each Gameweek has actually finished. A second,
independent check answers that for the same run set:

```bash
python scripts/check_release_freshness.py \
  --model-run baseline_... \
  --model-run baseline_... \
  --model-run baseline_...
```

For each model run, `validation/release_freshness.py` reads its source
`ingestion_run.captured_at`, `gameweek_snapshot.finished`/`data_checked` for the
target Gameweek, and the `fixture_snapshot` completion count, then reports:

- `fixtures.analytically_complete` -- every fixture assigned to that Gameweek is
  `finished`, the same rule `refresh_fpl_event_live.py --allow-analytically-complete`
  already uses;
- `fpl_finality.is_final` -- FPL's own `finished AND data_checked` flags;
- `drift_check_eligible` -- analytically complete but not yet FPL-final: a
  Gameweek whose provisional evidence could later be rebuilt and diffed against a
  final rerun (the rebuild itself is a separate, not-yet-implemented Sprint 5 item);
- `SNAPSHOT_STALE_RELATIVE_TO_NOW` -- the source snapshot is older than
  `--stale-after-hours` (default 24) AND the Gameweek's deadline has not yet
  passed; a completed Gameweek's snapshot age is not itself informative, so this
  flag is withheld once the deadline has passed.

None of the above fails the check -- provisional, incomplete, or stale evidence is
a normal mid-season state. It fails closed only when the source snapshot's
`captured_at` is after the deadline it was used to project for (a lookahead
hazard; `model_run` already enforces `as_of <= deadline` at the database level, so
only the snapshot's own capture time needs checking here) or a Gameweek is
entirely absent from its source snapshot's `gameweek_snapshot`/`fixture_snapshot`
rows.

## Sprint 5: approved calibration/uncertainty gate

The manifest and freshness checks above are silent on artifact APPROVAL status by
design -- the manifest only reports which Gameweeks are missing shadow lineage,
without failing on it. A third, independent check fails closed on that:

```bash
python scripts/check_release_approval.py \
  --model-run baseline_... \
  --model-run baseline_... \
  --model-run baseline_...
```

For each model run, `validation/release_approval.py` checks that its linked
uncertainty artifact is `status='approved'` AND was applied in `'active'` mode
(`model_uncertainty_lineage.application_mode`, set by `apply_uncertainty_artifact`
only when the artifact was approved at application time) with every
`player_fixture_projection.uncertainty` scalar populated, and that its linked
shadow-calibration artifact is also `status='approved'`.

Every artifact in this database currently has `status='shadow'`
(`docs/SPRINT4_UNCERTAINTY_AND_CALIBRATION.md`) -- both were fit and measured on
the same 2025/26 season they would be applied to, and promotion to `approved`
requires Sprint 4's own unchecked item: an independent-season or prospectively
frozen 2026/27 confirmatory evaluation. **This check is therefore expected to fail
for every release today.** That is the correct, honest state, not a defect --
this gate exists so a release automatically starts passing the day an artifact is
legitimately promoted, without any change to this script.

## Sprint 5: combined release validation

The three release-level checks above (manifest, freshness, approval) each run and
report independently. A thin, VALIDATE-ONLY orchestrator runs all three against
one named release and folds their verdicts together:

```bash
python scripts/validate_release.py \
  --model-run baseline_... \
  --model-run baseline_... \
  --model-run baseline_...
```

`validation/release_orchestration.py` calls `build_release_manifest`,
`check_release_freshness`, and `check_release_approval` against the same
`model_run_ids`/`database_path` and adds no validation of its own -- it
materialises and writes nothing; every named run must already exist. `passes`
requires manifest linkage AND freshness only; `approval_status` (`"approved"` /
`"shadow_only"`) is tracked and reported separately rather than folded into
`passes`, because collapsing it in would report failure for every release today
(no artifact has been promoted past `shadow` yet) and hide the two checks that
actually matter for day-to-day operation behind a gate nothing can currently
pass. The 100%-shortlist `decision_coverage` gate is deliberately NOT included
here -- it evaluates one specific decision command's own owned-squad/shortlist
pools, which do not exist until that command runs, and stays attached to each
command's own output instead.

`scripts/materialize_release.py` is the materialising counterpart. It runs the
official snapshot, player-identity bridge, availability, final prior-GW event-live,
appearance, team-strength, context, baseline, frozen-horizon, shadow-calibration,
uncertainty, and combined validation stages in one deterministic command. It exits
non-zero when manifest or freshness validation fails. `scripts/validate_release.py`
remains useful as the read-only validator for already named runs.

## Sprint 5: explicit research/shadow/production release state

`docs/PROJECTION_COVERAGE_AUDIT.md` and the Sprint 6/7/8 roadmap already assume
one explicit label -- `RESEARCH_ONLY`, or eventually `shadow`/`production` --
exists somewhere. Until now nothing computed it as structured output.
`validation/release_health.py` derives it read-only from gate reports already
computed elsewhere, adding no new check:

- `research` (label `RESEARCH_ONLY`) -- manifest linkage fails, freshness fails
  closed (a lookahead hazard or a Gameweek missing from its own source
  snapshot -- never mere staleness or incompleteness, which freshness already
  reports as non-failing flags), or any supplied `decision_coverage` gate is
  below its required 100%;
- `shadow` -- manifest and freshness (and any supplied coverage gate) all pass,
  but calibration/uncertainty are not yet APPROVED -- the expected state for
  every release today;
- `production` -- everything above passes AND calibration/uncertainty are
  APPROVED. No release in this database currently qualifies.

`scripts/validate_release.py` attaches this as a `health` object alongside its
existing manifest/freshness/approval sections (release-level only, with no
`decision_coverage` gates supplied, since those are specific to one decision
command's own output rather than to a release considered on its own). A caller
labelling one specific lineup/transfer/plan/initial-squad output can call
`determine_release_health` directly with that command's own `coverage_gate`
report to fold coverage into the same state.

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

`--audit-goalkeeper-reinvestment` (opt-in; runs one extra search pass) checks design principle 6's
"set-and-forget goalkeeper plus a cheap backup ... after the saved funds are optimally reinvested"
counterfactual: `decision/transfer_dominance.py` identifies the squad's own most expensive
goalkeeper (the one NOT already the higher-xPts starter), swaps it for the cheapest legal same-
position target, then takes the single best non-goalkeeper reinvestment with the freed bank --
charging each leg its own hit cost from the squad's actual free-transfer state rather than assuming
both are free. This is one NAMED two-transfer combo, not general multi-transfer search (a separate,
larger, not-yet-built Sprint 6 item); it reports whether the combo Pareto-dominates
`recommended.net_xpts_gain` without changing what `recommend_single_transfers` itself returns.

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

Every decision command above (`recommend_lineup.py`, `recommend_transfers.py`,
`plan_three_gameweeks.py`, `optimize_initial_squad.py`) attaches a `coverage_gate` object to its
JSON output, implementing the 100% owned-squad/optimizer-shortlist half of the coverage policy
`docs/PROJECTION_COVERAGE_AUDIT.md` already stated in prose (the 95% selectable-player half is
`audit_projection_coverage.py`). `validation/decision_coverage.py` reads each command's own
existing `excluded_missing_projection` diagnostics -- it does not change what any command searches
or computes -- and reports `passes`/`failing_pools` against the required 100%. `owned_squad` is
expected to always pass, since `decision/lineup_store.py` already fails closed on any missing owned
projection before a command ever runs; a shortlist pool (transfer targets, or a rolling/initial-
squad Gameweek pool) fails when the store silently excluded a missing-projection candidate, which
is exactly the failure mode `docs/INITIAL_SQUAD_OPTIMIZER.md`'s own diagnostic run described.

Every player entry each of those same four commands returns also carries a `transparency` object
(or `starters_transparency`/`captain_transparency` alongside an existing `starters`/`captain_fpl_id`
field where changing the field's own shape would break an existing consumer).
`validation/decision_transparency.py` reads `player_fixture_shadow_projection` and
`player_fixture_uncertainty` -- both measurement-only per
`docs/SPRINT4_UNCERTAINTY_AND_CALIBRATION.md` -- and reports `raw_xpts`,
`shadow_calibrated_xpts`, `lower_xpts`/`upper_xpts`/`predictive_rmse`, and `risk_band` next to the
production `expected_points`/`uncertainty` fields those commands already returned, so a raw,
calibrated, and interval view are all visible together rather than only the single production
number. This is purely additive display: it is never read by any `decision/*` search or ranking
logic, and a player absent from either shadow table simply reports `null` fields rather than
raising. Aggregation to one row per Gameweek/player when a double Gameweek spans two fixtures
mirrors `decision/lineup_store.py`'s own DGW handling: xPts-shaped values sum across fixtures,
uncertainty/RMSE-shaped values combine by root-sum-of-squares.

## Sprint 6: consuming only a release that passes the Sprint 5 gate

All four decision commands now call `validation.release_orchestration.
enforce_release_gate` on their own `model_run_id`(s) before doing any decision
work, and refuse to produce a recommendation when it fails. "Consume only an
approved Sprint 5 release" is interpreted as the manifest+freshness gate
(`orchestrate_release_validation`'s `passes`), not literal artifact
`status='approved'` -- every artifact is expected to be `shadow_only` today (see
the Sprint 5 approval gate above), so requiring literal approval would make
every decision command unusable before Sprint 4's confirmatory evaluation
exists. `recommend_transfers.py`/`plan_three_gameweeks.py`/
`optimize_initial_squad.py` build their `model_run_ids` tuple sorted by
ascending target Gameweek before calling the gate, since `--model-run GW=ID`
arguments are not guaranteed to arrive in Gameweek order and
`build_release_manifest` requires ascending order.

A failure raises `ReleaseGateFailure` (carrying the full report); each CLI
prints it and exits non-zero before touching squad or projection data. Each
command also accepts a loudly-labelled `--skip-release-validation` escape hatch
for local development, mirroring the existing `--allow-provisional` precedent in
`scripts/refresh_fpl_event_live.py` -- it is not intended for an operational
recommendation. When the gate is enforced, each command's JSON output also
carries a `release_health` object from `validation.release_health.
determine_release_health`, folding in that same command's own `coverage_gate`
so the reported state (`research`/`shadow`/`production`) reflects both release
health and this specific decision's own coverage.

## Sprint 6: expected autosub value

`decision/lineup.py`'s `total_xpts` only ever sums the 11 starters plus the captain bonus -- every
bench player contributes exactly zero, even though real FPL autosubs a blanking starter for the
first eligible bench player. `decision/autosub.py` computes each bench slot's OWN expected xPts
contribution under FPL's actual autosub rule (verified against
[LiveFPL](https://www.livefpl.com/blog/fpl-auto-subs) and
[Fantasy Football Scout](https://www.fantasyfootballscout.co.uk/2023/06/01/how-do-substitutes-work-in-fpl-and-what-are-autosubs)):
a player is autosubbed only on exactly 0 minutes for the whole Gameweek; the starting goalkeeper can
only be replaced by the bench goalkeeper (and only if that bench goalkeeper is not also blanking);
outfield starters are checked against the bench in bench order, skipping a substitution that would
break legal formation (reusing `decision.lineup.is_legal_starting_xi`'s own definition so this can
never silently diverge from production lineup legality); each bench player is used for at most one
substitution. Given every starter's own independent blank probability
(`1 - appearance_probability`), it enumerates blank patterns and weights each bench player's xPts by
the probability of the specific pattern that uses them -- an expected value, not a simulated outcome.

`PlayerGameweekProjection` gained an `appearance_probability` field (start + substitute appearance
probability, combined across DGW fixtures as `1 - product(1 - appearance_i)` via
`decision.lineup_store.combine_appearance_probability`) to carry this from
`player_fixture_projection`'s existing `start_probability`/`substitute_appearance_probability`
columns through every decision `_store.py` module. `recommend_lineup.py`'s output carries the result
as `expected_autosub_value`, explicitly informational: it is never added to `total_xpts` or any
other production score, matching the same measurement-only boundary Sprint 4's shadow calibration
and uncertainty artifacts already observe.

## P0: explicit role state

A raw `start_probability` number forces a manager to reverse-engineer whether a player reads as a
safe starter, a rotation risk, or effectively a bench spot. `validation/role_state.py` derives one
explicit category -- `unavailable`, `unknown`, `likely_bench`, `rotation`, `likely_starter` -- plus a
plain-language reason, from inputs that already exist: the player's own resolved eligibility
(`player_availability_resolution.is_eligible`, read from the SAME `availability_resolution_run` the
model run's own appearance projection used) and `start_probability`/`appearance_probability` from
`player_fixture_projection`, combined across double-Gameweek fixtures the same way
`combine_appearance_probability` already does.

Precedence is deliberate: a resolved-ineligible player reads `unavailable` even if their own
projection is stale-high (e.g. a late injury confirmed after the projection ran); missing evidence
(unresolved eligibility, or a player entirely absent from `player_fixture_projection` for this run)
reads `unknown`, never silently folded into `likely_bench` -- evidence-missing and evidence-low are
different conditions a manager needs to tell apart. Every decision-producing CLI script attaches
`role_state` to every player it returns exactly the way each already attaches `transparency`
(`decision_transparency.py`) -- read-only, additive, and never consumed by `decision/*` search or
ranking logic: `recommend_lineup.py` (per-player), `recommend_transfers.py` (owned squad and
transfer targets), and `plan_three_gameweeks.py`/`optimize_initial_squad.py` (per-Gameweek, since a
player's role state can differ across the horizon). This coverage is what the P0 sign-off item
("no owned-player lineup or transfer decision can depend on a material role conflict without a
visible warning") verifies -- auditing the four scripts found `recommend_transfers.py`,
`plan_three_gameweeks.py`, and `optimize_initial_squad.py` had `transparency` but not yet
`role_state`, which this wiring closed.

## P0: retrospective material conflict audit

A model can be well-calibrated in aggregate while still being badly wrong for one specific player
in one specific Gameweek -- exactly the two shapes P0 names: "a 60+ minute start with low projected
start probability, or a zero-minute available player with a high projected appearance probability".
`validation/material_conflict.py`'s `audit_material_conflicts` compares one completed `model_run`'s
own stored `player_fixture_projection` against the SAME Gameweek's own FINAL
`fpl_event_live_run`/`player_gameweek_stat` outcome (`event_finished AND data_checked`; a provisional
event, or a Gameweek/official-snapshot mismatch between the two runs, raises rather than silently
comparing unrelated data). `LOW_START_PROBABILITY_THRESHOLD`/`HIGH_APPEARANCE_PROBABILITY_THRESHOLD`
deliberately reuse `role_state.py`'s own `ROTATION_THRESHOLD`/`LIKELY_STARTER_THRESHOLD` constants,
so "low"/"high" mean the same number a manager already sees in a player's role state, not an
independently drifting pair of thresholds.

```bash
python scripts/audit_material_conflicts.py \
  --model-run-id baseline_... --live-run-id fpl_live_gw...
```

This is retrospective and read-only: it changes no projection, calibration, or recommendation. It
is the audit layer the prospective decision-safety warning below (`role_scenario_sensitivity.py`)
is intended to eventually validate against a track record of Gameweeks -- material-conflict audit
tells you a past `ROTATION`-labelled player's outcome actually mattered; role-scenario sensitivity
tells you, before the deadline, when a CURRENT recommendation depends on one.

## P0: retrospective appearance observation classification

A raw `played`/`minutes`/`starts` triple leaves every 0-minute player looking the same, whether they
were injured, not yet registered, or simply an unused substitute. `validation/appearance_observation.py`'s
`load_appearance_observations` classifies one completed, FINAL Gameweek's own outcome
(`fpl_event_live_run`/`player_gameweek_stat`) into one of six named categories -- `starter`,
`substitute`, `unavailable`, `not_yet_eligible`, `no_team_fixture`, or
`unused_substitute_or_not_in_squad` -- using only data already in this database: the matching
`availability_resolution_run` for eligibility, `player_snapshot` for registration, and
`fixture_snapshot` for whether the player's team even had a fixture that Gameweek.

FPL's `event/{gw}/live` endpoint returns a stats row for every player in the game whether they played
or not, but it does not expose the 20-man matchday squad or bench list -- so an unused substitute
genuinely cannot be told apart from a player left out of the squad entirely using data this pipeline
has access to. Rather than guess, both collapse into the single named
`unused_substitute_or_not_in_squad` bucket, which is itself the honest answer: a manager reading it
knows exactly what is and is not known, rather than being shown a confident label the underlying data
cannot support.

```bash
python scripts/classify_appearance_observations.py --live-run-id fpl_live_gw...
```

This is retrospective and read-only, the same boundary `material_conflict.py` observes: it changes no
projection, calibration, or recommendation, and requires a FINAL live run
(`event_finished AND data_checked`).

## P0: prospective role-scenario sensitivity

A raw `role_state` per player still leaves a manager to work out, unassisted, whether any given
`ROTATION` label actually changes what they should do. `decision/role_scenario_sensitivity.py`'s
`evaluate_role_scenario_sensitivity` answers that directly: for every squad member `role_state.py`
marks `ROTATION` (never `LIKELY_STARTER`, `LIKELY_BENCH`, `UNAVAILABLE`, or `UNKNOWN` -- perturbing
those would not plausibly change anything), it re-runs the exact same `recommend_lineup` search with
that one player's projection replaced by a "blanks entirely" counterfactual (0 expected points, 0
appearance probability -- exactly FPL's own 0-minute autosub condition), one player at a time, and
checks whether the starting XI or captain would change.

`recommend_lineup.py`'s output carries the result as `role_scenario_sensitivity`: `label` is either
`"stable"` or `"sensitive"`, and `scenarios_that_change_the_recommendation` names exactly which
player(s) drive a `sensitive` label, so a manager sees which specific rotation risk the recommendation
is conditional on rather than an unconditional "best option". Like `role_state` and
`expected_autosub_value`, this is read-only and additive: it never alters what `recommend_lineup`
itself returns for the base scenario, only labels it.

## P0: deadline-safe current-season player-rate update (materialize-only)

`baseline_pipeline.py`'s in-season path still scores every Gameweek from the frozen previous-season
`player_rate_history` (flagged `FROZEN_PREVIOUS_SEASON_PLAYER_RATES`) -- a player's current-season
form never updates their rate inputs. `model/current_season_rates.py`'s
`materialize_current_season_rates` closes that gap as a NEW, separate lineage
(`current_season_player_rate_run`/`current_season_player_rate`) rather than repurposing
`player_rate_history_run`/`player_rate_history`, which stay Vaastav-previous-season-import-only
(enforced by their own foreign key into `player_fixture_history_import_run`).

Deadline safety mirrors `material_conflict.py`'s own finality gate: a Gameweek enters the rate
window only if its `fpl_event_live_run` was already FINAL (`event_finished AND data_checked`) at or
before the run's own `as_of`, and only if it is strictly before the target `as_of_gameweek`. A
provisional Gameweek -- or the Gameweek being scored itself -- can never leak into its own rate
window; this is what "no retrospective post-match xP leakage" means concretely here.

Shrinkage blends each per-90 rate toward that SAME player's own previous-season rate (the latest
matching `player_rate_history` row for their `player_code`), weighted by minutes:

```
blended = (current_minutes * current_rate + K * prior_rate) / (current_minutes + K)
```

`K` is `SHRINKAGE_PRIOR_MINUTES = 900.0`, reusing `baseline_pipeline.py`'s own
`PRIOR_REFERENCE_MINUTES` rather than an independently drifting constant. A player with no
previous-season rate history at all is NOT rescued by an invented prior here -- the raw
current-season rate is stored with an explicit `NO_PREVIOUS_SEASON_RATE_HISTORY` flag;
`baseline_pipeline.py`'s own existing empirical cohort-average prior remains the fallback for that
case (see the Tzolis-shaped regression case above).

```bash
python scripts/materialize_current_season_rates.py \
  --source-ingestion-run-id fpl_... --season 2026-27 \
  --as-of-gameweek 3 --as-of 2026-09-01T00:00:00+00:00
```

Two deliberate scope limits: xGC is NOT a per-player field here (it is a team-level input,
`team_strength_projection`, in this model's architecture, never a per-player rate); and cards/BPS
are not yet included, only xG/xA/DefCon/saves. And, per an explicit decision, **this run is
materialize-only** -- `baseline_pipeline.py`'s in-season path does not yet consume it, so
`final_xpts` is unaffected. Wiring a current-season rate into production scoring is deferred to a
separate, explicitly-approved change that should go through the existing shadow/calibration release
path (`validation/release_health.py`) rather than changing `final_xpts` outright without one.

The first presentation boundary is a generated local HTML dashboard. It reads scenario CSVs,
official identity/status from one pinned ingestion, and exactly three compatible frozen model
runs. It is read-only with respect to DuckDB and writes no decision back into projection tables.
Scenario tabs expose transfer differences, budget legality, per-player xPts, and missing-data
flags. Lineup recommendations are withheld unless all 15 players are covered, preserving the
model's rule that a projection gap is not a zero. See `docs/SQUAD_DASHBOARD.md`.

Implementation order remains deliberately simple and auditable:

1. select an initial preseason squad from public data (implemented as a bounded beam search);
2. validate and version the exact 15-player manager state;
3. enumerate a legal starting XI, bench order, captain, and vice-captain from that fixed squad;
4. enumerate no-transfer and single-transfer alternatives using exact sale value and bank;
5. add rolling three-Gameweek scoring, FT state, and uncertainty (implemented as bounded beam
   search; awaiting future-GW projections and backtesting);
6. add multi-transfer/chip state and introduce a solver only when the expanded search requires it.
