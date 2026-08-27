# FPL Model

An explainable Fantasy Premier League projection engine built around auditable expected-points components, contextual adjustments, and reproducible data pipelines.

## Project goal

The first objective is **not** to build a black-box ML predictor. We will first reproduce the Benchwarmers 2026/27 spreadsheet model in Python, component by component, then test whether additional contextual features improve out-of-sample predictions.

Planned contextual layers include:

- promoted-team / league-translation priors
- manager regime changes
- player tactical-role changes
- World Cup / preseason readiness
- fixture congestion and European competition
- uncertainty and calibration

## Design principles

1. **Deadline-safe data only** for backtests. No same-GW lookahead.
2. **Formation is metadata; player role is a feature.** A nominal RB may behave like a high wing-back or an inverted midfielder.
3. **Context changes causal inputs, not xPts directly.** For example, World Cup readiness should first affect start probability / expected minutes rather than applying an arbitrary points multiplier.
4. **Every extension must beat the baseline in backtesting.**
5. **Raw scraped/downloaded data is not committed.** Pipelines should make it reproducible.
6. **A recommender must beat common structural counterfactuals.** At minimum, compare against a
   set-and-forget goalkeeper plus a cheap backup, cheap-bench reinvestment, and premium-captain
   scenarios before calling a squad optimal.
7. **Implemented does not mean operationally ready.** Coverage, calibration, uncertainty, search
   correctness, and squad-economics sanity gates must pass separately.

## Current scope: projection release and decision validation

This repository currently includes:

- official FPL API ingestion
- Vaastav historical/current-season ingestion
- canonical identifiers and records
- preseason availability schema
- spatial/heatmap fingerprint primitives
- season/model configuration
- network-free unit tests for core transformations
- component-level Benchwarmers baseline projections
- an end-to-end GW1 baseline materialiser with immutable input lineage and explicit gaps
- explicit fixture/DGW composition and one-time home/away adjustment
- deadline-safe walk-forward fold and metric primitives
- a genuine walk-forward backtest of the replicated 11-component model against in-season Vaastav
  history (Vaastav-only team strength, no workbook)
- a deadline-safe GW2+ refresh that separates analytically complete official playing-time evidence
  from FPL's later whole-Gameweek finalisation
- a frozen three-Gameweek projection horizon plus lineup, transfer, and squad-scenario prototypes

Selectable-player projection coverage now passes its explicit gate. The next milestone is a
versioned production projection release: freshness, finality, coverage, calibration, uncertainty,
and drift checks must produce one auditable release manifest before any downstream surface calls a
result "best". Decision hardening, a stable squad-rating contract, and the three application menus
then consume that approved release in dependency order.

## Recommender status: research prototype only

The lineup, transfer, rolling-planner, and initial-squad code paths are implemented, but the
initial-squad output is **not operationally approved**. A deadline-time diagnostic run on 22 August
2026 exposed three concrete failures:

- only 404 of 600 snapshot players had complete GW1--GW3 projections; missing/new/cheap
  players were therefore underrepresented in the optimizer's choice set;
- the approximate beam search returned a 188.68 xPts squad with two GBP 5.0m goalkeepers and an
  GBP 8.0m Watkins benched in GW1 and GW2;
- a manually locked Raya/Dubravka structure scored 189.09 xPts at the same GBP 100.0m budget, so
  the published search result was dominated by a legal counterfactual.

Baseline policy v7 closes the first failure with empirical, flagged position/price/cohort priors:
583 of 600 players are projected and selectable-player coverage is 100%; the remaining 17 are all
roster-blocked. A retrospective sanity simulation still selected Watkins on the bench in GW1 and
GW2, so the coverage fix does **not** remove the decision-layer warning.

Until the model-release, decision-policy, and rating gates below pass, generated squads must be
labelled `RESEARCH_ONLY`, include coverage diagnostics, and show the best common counterfactuals
alongside the nominal result.

## Repository layout

```text
config/                    Model and season configuration
data/
  raw/                     Local upstream downloads (gitignored)
  processed/               Generated canonical tables (gitignored)
  annotations/             Small hand-reviewed tactical/context annotations
src/fpl_model/
  ingest/                  FPL, Vaastav, Understat/preseason adapters
  schema/                  Canonical records and validation
  tactics/                 Spatial fingerprints and tactical roles
  context/                 Manager, promotion, tournament, congestion features
  model/                   xPts component engine (next sprint)
  validation/              Backtests and calibration (next sprint)
tests/                     Deterministic unit tests
legacy/                    Notes on the early notebook prototype
```

## Quick start

```bash
python -m venv .venv
```

Windows:

```bash
.venv\Scripts\activate
```

macOS/Linux:

```bash
source .venv/bin/activate
```

Install:

```bash
pip install -e ".[dev]"
pytest
```

Example FPL API smoke run:

```bash
python -m fpl_model.ingest.fpl
```

Example Vaastav load:

```python
from fpl_model.ingest.vaastav import VaastavClient

client = VaastavClient()
players = client.cleaned_players("2026-27")
```

Deadline-safe pipeline smoke test (not the Benchwarmers benchmark):

```bash
python scripts/backtest_smoke.py --season 2025-26
```

Genuine walk-forward backtest of the replicated 11-component model, scored in-season from
Vaastav-only data. Deadline safety requires both `gameweek < N` and
`kickoff_time + outcome_delay <= target_deadline`, so a postponed fixture cannot leak into a later
prediction. The output also reports `matched_naive_metrics`, an expanding player-points-mean
comparator scored on exactly the same rows as the model, for a fair naive baseline (see
`docs/PIPELINE_ARCHITECTURE.md`):

```bash
python scripts/backtest_benchwarmers.py --season 2025-26
```

Paired, gameweek-cluster bootstrap uncertainty of the model's MAE/RMSE advantage over
`matched_naive_metrics` -- whether that advantage is a stable signal or noise from one season's 36
gameweeks (see `docs/PIPELINE_ARCHITECTURE.md`):

```bash
python scripts/analyze_backtest_uncertainty.py --season 2025-26
```

Walk-forward xPts calibration slope/intercept, fit strictly on prior gameweeks and compared
out-of-sample against both the raw model and `matched_naive_metrics` -- measurement only, not
applied to any production projection (see `docs/PIPELINE_ARCHITECTURE.md`):

```bash
python scripts/assess_backtest_calibration.py --season 2025-26
```

Walk-forward calibration of the appearance model's own predictions (`start_probability` against
realised starts, `expected_minutes` against realised minutes), isolating whether xPts
overprediction traces back to appearance or to the per-90 rate components -- measurement only, not
applied to any production projection (see `docs/PIPELINE_ARCHITECTURE.md`):

```bash
python scripts/assess_appearance_calibration.py --season 2025-26
```

Segment the appearance model's own calibration bias by fixed start_probability/expected_minutes
bands, position, and season phase, across the primary cohort plus xPts-scored and xPts-high-band
sensitivity cohorts -- measurement only, not applied to any production projection (see
`docs/PIPELINE_ARCHITECTURE.md`):

```bash
python scripts/diagnose_appearance_segments.py --season 2025-26
```

Causal walk-forward execution of three appearance calibration policies (raw / global /
high-end-only shrinkage above start_probability 0.8), each policy's predicted_xpts recomputed and
re-scored -- fit/threshold derived only from strictly-prior, deadline-safe gameweeks at each step.
This is an EXPLORATORY, same-season (2025-26) comparison, not an independent confirmatory
backtest: the decision to test high-end-only shrinkage was itself informed by the segment
diagnostic's own 2025-26 findings (see `docs/PIPELINE_ARCHITECTURE.md`):

```bash
python scripts/backtest_appearance_calibration_policies.py --season 2025-26
```

Audit whether historical Vaastav snapshots existed before each inferred deadline:

```bash
python scripts/audit_vaastav_snapshots.py --season 2025-26
```

Initialise the gitignored local snapshot database:

```bash
python scripts/init_local_db.py
```

Build an immutable official-FPL/Vaastav player identity bridge from a pinned `players_raw.csv`:

```bash
python scripts/import_player_identity_bridge.py --help
```

The bridge uses the shared stable `player_code`; names are audit signals and never join keys. See
`docs/PLAYER_IDENTITY_BRIDGE.md`.

Audit and classify every player withheld from one immutable baseline run:

```bash
python scripts/audit_projection_coverage.py --model-run baseline_...
```

The report separates root cause from promoted/current-only/cheap-enabler cohorts and enforces the
Sprint 3 coverage gate without inventing fallback xPts. See `docs/PROJECTION_COVERAGE_AUDIT.md`.

Baseline policy v7 resolves supported gaps with empirical priors derived from players in the same
position and price band. Missing-appearance priors prefer promoted/new-signing/returning cohorts
when at least five comparable rows exist. Every fallback records its cohort, scope, and sample size;
roster-blocked players remain gaps. See `docs/EMPIRICAL_PROJECTION_PRIORS.md`.

Import an immutable current-manager squad snapshot after refreshing official FPL data:

```bash
python scripts/import_squad_snapshot.py --help
```

The manual CSV contains only private manager state (FPL IDs, purchase/selling prices, squad order,
captain, and vice-captain); player identity and current market state are joined from a pinned FPL
snapshot. See `docs/SQUAD_TRACKER.md` for the full contract and 2026/27 rules validation.

Recommend the best legal XI and captaincy from a completed same-Gameweek model run:

```bash
python scripts/recommend_lineup.py --help
```

Compare no transfer with every affordable, legal single transfer for that Gameweek:

```bash
python scripts/recommend_transfers.py --help
```

The transfer command is deliberately an explainable one-Gameweek baseline. It uses the manager's
actual selling prices, bank, free-transfer state, and a four-point hit when necessary.

Build a rolling three-Gameweek plan from three explicit, frozen model runs:

```bash
python scripts/plan_three_gameweeks.py --help
```

Generate a standalone browser dashboard to compare named squad scenarios against those same
three frozen runs:

```bash
python scripts/build_squad_dashboard.py --help
```

The dashboard reconciles private squad CSVs to a pinned official FPL snapshot, shows transactions,
per-player coverage and GW1-GW3 xPts, and withholds lineup optimization when any player projection
is missing. See `docs/SQUAD_DASHBOARD.md`.

Create the anchor GW plus GW+1/GW+2 fixture projections from one frozen preseason baseline:

```bash
python scripts/project_frozen_horizon.py --anchor-model-run-id baseline_...
```

Choose a legal public-data initial squad over those three frozen Gameweeks, without requiring a
manager screenshot or private squad state:

```bash
python scripts/optimize_initial_squad.py --help
```

The initial-squad search enforces the £100.0m budget, 2/5/5/3 positions, and three-player club cap,
then re-optimizes XI and captaincy per GW. It is a bounded candidate/beam search, not a certified
global optimum; see `docs/INITIAL_SQUAD_OPTIMIZER.md`.

The planner follows the intended three-GW review cadence, with injury, suspension, real-world
transfer, or material role change documented as emergency replan triggers. The search is a
candidate-pruned beam search rather than a claim of a global optimum. Horizon projection rescoring
uses each future GW's own fixture/opponent/venue while freezing appearance, player-rate, and team-
strength inputs to the anchor `as_of`; these assumptions are flagged on every future row.

Fetch and persist a timestamped official FPL player/fixture snapshot:

```bash
python scripts/refresh_fpl_snapshot.py --season 2026-27
```

Resolve deadline-safe availability for the target gameweek:

```bash
python scripts/resolve_availability.py --gameweek 1
```

Reviewed injury/eligibility corrections can be appended with
`scripts/add_availability_override.py`. The command requires a timestamped source and rationale;
run a new FPL refresh afterwards when instructed so the override enters a new immutable resolution.

For GW2 and later, archive only final, checked official event data, shrink current-season
appearance evidence toward the previous-season prior, materialise descriptive context, and rebuild
the three-GW horizon with:

```bash
python scripts/refresh_fpl_event_live.py --help
python scripts/project_inseason_appearance.py --help
python scripts/materialize_context_features.py --help
python scripts/project_inseason_baseline.py --help
```

The exact command order, fail-closed finality gate, context annotation contract, and frozen-prior
limitations are documented in `docs/INSEASON_REFRESH.md`.

After exporting the workbook's resolved previous-season appearance fields, import and materialise
the GW1 appearance projection with:

```bash
python scripts/import_appearance_history.py \
  --csv data/raw/workbooks/benchwarmers_appearance_history_2025_26.csv \
  --season 2025-26 \
  --source-label "MODEL.xlsx resolved previous-season appearance fields"
python scripts/project_preseason_appearance.py --gameweek 1 --previous-season 2025-26
```

New signings or reviewed role changes that are absent from the workbook can receive a sourced,
gameweek-specific conditional playing-time scenario through
`scripts/add_appearance_scenario_override.py`. Missing history is otherwise retained as a flagged
gap rather than fabricated as zero.

Archive a pinned Vaastav revision and materialise the previous-season player rate windows with:

```bash
python scripts/import_vaastav_player_history.py --season 2025-26
```

For current players still missing a usable previous-PL rate, export and import a small targeted
evidence sheet. This stores source/comparability metadata only; it does not yet alter xPts:

```bash
python scripts/export_player_rate_evidence_template.py --help
python scripts/import_player_rate_evidence.py --help
```

Academy samples use `comparability_class=academy_youth`; sourced role evidence without meaningful
senior rate statistics uses `role_only`. See `docs/PLAYER_RATE_EVIDENCE.md`.

The separate team-strength workbook export contract is documented in
`docs/research/CLAUDE_TEAM_STRENGTH_EXPORT_PROMPT.md` and consumed by
`scripts/import_team_strength.py`.

Once appearance, player-rate, and team-strength runs exist, materialise the explainable GW1
player-fixture baseline with:

```bash
python scripts/project_preseason_baseline.py --gameweek 1
```

The runner stores all eleven component values, applies home/away once, and records missing player
history as a gap rather than assigning zero xPts. Optional run-ID arguments pin an exact upstream
lineage when reproducing a result.

Link one Gameweek horizon's full upstream lineage (official snapshot, identity bridge,
availability, appearance, player rates, team strength, context, shadow calibration/uncertainty)
into one immutable, content-hashed release manifest. This does not itself decide freshness,
coverage, or calibration; it fails closed only when the named runs do not cohere as one horizon
(shared snapshot, shared frozen `as_of`, complete lineage per Gameweek):

```bash
python scripts/build_release_manifest.py \
  --model-run baseline_... --model-run baseline_... --model-run baseline_...
```

Check freshness, fixture-completion, and FPL-finality for the same run set. This reports staleness,
incomplete fixtures, and non-final Gameweeks as flags rather than failures; it fails closed only on
a lookahead hazard (source snapshot captured after its own deadline) or a Gameweek missing entirely
from its snapshot:

```bash
python scripts/check_release_freshness.py \
  --model-run baseline_... --model-run baseline_... --model-run baseline_...
```

Check that every released projection row has APPROVED (not merely shadow) calibration and
uncertainty lineage. Every artifact in this database is currently `shadow`, so this is expected to
fail closed until Sprint 4's independent-season 2026/27 confirmatory evaluation promotes one -- see
`docs/SPRINT4_UNCERTAINTY_AND_CALIBRATION.md`:

```bash
python scripts/check_release_approval.py \
  --model-run baseline_... --model-run baseline_... --model-run baseline_...
```

Run the manifest, freshness, and approval gates together against one named release:

```bash
python scripts/validate_release.py \
  --model-run baseline_... --model-run baseline_... --model-run baseline_...
```

For the normal GW2+ refresh, the deterministic end-to-end command now fetches the official
snapshot and final prior-GW event data, builds identity/availability/appearance/team-strength/
context lineage, freezes GW/GW+1/GW+2, attaches shadow or approved artifacts, and runs the same
release gates:

```bash
python scripts/materialize_release.py --help
```

Export a passing release for the stateless web app and compare it with an earlier provisional
release (optionally rescoring an owned squad) with:

```bash
python scripts/export_web_release.py --help
python scripts/compare_release_drift.py --help
```

See `docs/PIPELINE_ARCHITECTURE.md` for the weekly refresh, injury/eligibility snapshot, and Excel
output contract.

## Upstream data

The project uses or may use data from:

- the official Fantasy Premier League API
- `vaastav/Fantasy-Premier-League` as a historical/current-season upstream dataset
- Understat for xG/xA-related data where appropriate
- spatial/preseason providers after a reproducibility and terms-of-use review

See `THIRD_PARTY_NOTICES.md`.

## Roadmap

### Sprint 1 — Data foundation (complete)

- [x] Repository/package structure
- [x] Official FPL API adapter
- [x] Vaastav adapter
- [x] Canonical schemas
- [x] Spatial fingerprint primitive
- [x] Chelsea structured-preseason heatmap feasibility test (provider unavailable; no bypass)
- [x] Canonical player ID bridge across official FPL and Vaastav

### Sprint 2 — Benchwarmers baseline replication (complete)

- [x] Reproduce appearance/start/minutes logic
- [x] Goal / assist components
- [x] Clean sheet / goals-conceded components
- [x] Saves / cards / bonus / DefCon
- [x] Fixture and home-away adjustments
- [x] Golden tests against spreadsheet outputs

### Sprint 3 — In-season projection inputs and evidence foundation

**Status:** engineering complete; context activation remains evidence-gated.

- [x] Reach an explicit coverage gate for selectable players (target: at least 95%, plus 100% of
      every optimizer shortlist and selected squad)
- [x] Add explicit promoted-team, new-signing, returning-player, and cheap-enabler priors instead
      of treating missing previous-PL history as near-zero ability or excluding it silently
- [x] Deadline-safe manager-regime feature store with reviewed source lineage
- [x] World Cup/preseason readiness features plus final current-season playing-time evidence
- [x] Domestic congestion / rest-day features, with unsupported DGW allocation withheld
- [x] Tactical-role fingerprints and position-change diagnostics
- [x] Deadline-safe GW2+ refresh of official event history, appearance, context, and horizon inputs
- [x] Separate `analytically complete` fixture evidence from whole-Gameweek FPL finalisation;
      preserve an explicit provisional quality flag and immutable final rerun path
- [x] Paired, gameweek-clustered ablation acceptance harness for every context layer
- [ ] Populate complete reviewed annotations and non-PL workload evidence
- [ ] Prove incremental out-of-sample value, then activate only the supported causal adjustments

### Sprint 4 — Model validation, calibration, and uncertainty

**Status:** shadow engineering active; production activation gated.

- [x] Walk-forward fold/metric primitives
- [x] Genuine 11-component walk-forward backtest (2025-26, in-season)
- [x] Segment diagnostics (position/gameweek/minutes/start-probability/xPts bands)
- [x] Paired gameweek-cluster bootstrap uncertainty of model-vs-naive MAE/RMSE advantage
- [x] Walk-forward xPts calibration assessment (measurement only)
- [x] Walk-forward appearance-model calibration assessment (measurement only)
- [x] Appearance-model bias segment diagnostic
- [x] Exploratory same-season appearance-calibration policy backtest
- [ ] Independent-season or prospectively frozen 2026-27 confirmatory evaluation
- [ ] Apply supported appearance calibration to production projections
- [ ] Apply supported xPts calibration to production projections
- [x] Per-player/per-fixture residual intervals and empirical risk bands in shadow mode
- [x] Immutable xPts calibration artifacts and counterfactual shadow projections
- [x] Reviewed penalty/non-penalty xG decomposition that withholds unverified splits
- [x] Strictly prior-outcome walk-forward interval coverage by position
- [x] Prospective cohort plumbing for premiums, cheap enablers, promoted-team players, new/current-
      only players, and position changes
- [ ] Validate calibration and uncertainty on final 2026/27 outcomes for every prospective cohort
- [ ] Approve and activate scalar uncertainty only after overall and weakest-segment gates pass

### Sprint 5 — Production projection release gate

This sprint turns upstream model artifacts into one approved, auditable release. It must complete
before decision outputs can lose the `RESEARCH_ONLY` label.

- [x] Create one immutable release manifest linking official snapshot, event-live evidence,
      availability, appearance, player rates, team strength, context, model runs, and horizon runs
- [x] Record freshness, fixture-completion, FPL-finality, and provisional-to-final drift-eligibility
      checks (the drift rebuild-and-compare itself is the separate item below)
- [x] Require 100% coverage for the owned squad, optimizer shortlist, and every retained decision
      path; retain the global selectable-player coverage gate
- [x] Attach approved calibration and uncertainty lineage to every released player-fixture
      projection; fail closed when required artifacts are absent or stale (the gate is
      implemented and tested; it fails closed today because no artifact has been promoted past
      `shadow` yet -- see Sprint 4's own unchecked confirmatory-evaluation item)
- [x] Keep raw mean xPts, calibrated xPts, uncertainty, and data-quality flags separately visible
      rather than hiding them inside one opaque score
- [x] Add a deterministic pre-deadline orchestration command that materialises and validates the
      complete GW, GW+1, and GW+2 release (`scripts/materialize_release.py`, including snapshot,
      identity bridge, final event-live evidence, appearance, strength, context, horizon,
      calibration/uncertainty attachment, manifest, freshness, approval, and health output)
- [x] Rebuild an analytically complete provisional release after official finalisation and report
      whether any material player, lineup, cumulative squad outlook, or single-transfer decision
      changed (`scripts/compare_release_drift.py`; percentile rating remains Sprint 7 scope)
- [x] Add release-level smoke tests, machine-readable health output, and explicit
      `research`/`shadow`/`production` approval states (`validation/release_health.py` plus
      `scripts/validate_release.py`'s `health` output; "smoke tests" are the deterministic
      pytest suite each gate module already carries, matching this repo's existing convention)
- [ ] Production projection sign-off: no unresolved freshness, coverage, calibration, uncertainty,
      or lineage gate

### Sprint 6 — Decision engine hardening and operational safety

The objective remains transparent expected FPL points. A branded application score must not replace
or silently change the decision objective.

- [x] Immutable manager-squad snapshot foundation
- [x] Exhaustive legal single-Gameweek lineup recommender
- [x] Explainable single-transfer comparison
- [x] Rolling three-Gameweek planner and frozen-run contract
- [x] Frozen preseason GW+1/GW+2 fixture rescoring
- [x] Approximate initial-squad beam-search prototype (`RESEARCH_ONLY`)
- [x] Add locked and excluded player constraints for the initial-squad optimizer (`--lock`/
      `--exclude`, for example Haaland/no-Haaland and set-and-forget/rotating goalkeeper
      comparisons); the same constraint concept for the rolling multi-Gameweek transfer planner
      (locking/excluding a transfer target rather than an initial pick) is a separate, not yet
      built extension
- [x] Replace or audit the beam search with an exact/dominance-checked optimizer (audited, not
      replaced: `decision/initial_squad_dominance.py` plus `optimize_initial_squad.py
      --audit-dominance` runs two named structural counterfactuals -- cheap goalkeeper pair,
      cheap bench reinvestment -- and flags Pareto-dominance against the beam's own result; it
      does not certify a global optimum)
- [x] Add counterfactual bench economics: cheapest legal bench plus optimal reinvestment must be
      compared against every expensive-bench recommendation (`initial_squad_dominance.py`'s
      `cheap_bench_reinvestment` counterfactual for preseason picks); this checks one named
      counterfactual, not every possible bench structure
- [x] Add goalkeeper sanity: a rotating pair must beat set-and-forget plus cheap backup after the
      saved funds are optimally reinvested (`transfer_dominance.py`'s
      `--audit-goalkeeper-reinvestment` for an existing manager squad, plus
      `initial_squad_dominance.py`'s `cheap_goalkeeper_pair` for preseason picks); both charge the
      swap's own hit cost from actual free-transfer state rather than assuming it is free
- [x] Add premium sanity: an expensive player who is rarely started or captained must prove higher
      marginal horizon value than the best cheaper structure (`initial_squad_dominance.py`'s
      `premium_starter_reinvestment` counterfactual: the most expensive premium-priced player
      never captained across the retained horizon, excluded and compared by Pareto-dominance)
- [x] Model expected autosub value rather than treating bench players as either perfect future
      rotation pieces or zero-value reserves (`decision/autosub.py`, replicating FPL's real
      autosub rule -- 0-minute blanks, GK-for-GK only, bench order, formation-legal substitutions
      -- as an expected value over independent blank probabilities; informational only, never
      added to `total_xpts`)
- [x] Integrate planned transfers into the initial-squad horizon instead of freezing all 15
      players (`optimize_initial_squad.py` now rescores the top frozen-squad shortlist over legal
      roll/single-transfer paths for GW+1 and GW+2; `--freeze-squad-horizon` reproduces the older
      hold-all-15 counterfactual)
- [ ] Add multi-transfer and chip-aware optimization
- [ ] Add optional ownership/EO and risk-adjusted objectives separately from mean xPts
- [x] Consume only an approved Sprint 5 projection release and fail closed when its gates fail
      (interpreted as the manifest+freshness gate, not literal artifact `status='approved'` --
      see `docs/PIPELINE_ARCHITECTURE.md`; every decision command now calls
      `enforce_release_gate` before producing a recommendation, with a loudly-labelled
      `--skip-release-validation` escape hatch for local development only)
- [ ] Walk-forward evaluate the full planner and decision policy before operational use
- [ ] Operational sign-off: no dominated squad, all sanity checks pass, and every recommendation
      includes marginal-value explanations

### Production critical path — authoritative execution order

The numbered research sprints above remain the engineering record, but their unfinished items are
no longer treated as one sequential launch checklist. The production path is deliberately smaller:

1. make owned-squad decisions safe when role/minutes assumptions are uncertain;
2. expose the existing lineup, three-Gameweek, and validated single-transfer capabilities through
   the three primary menus;
3. operate a clearly labelled closed alpha and paid-founder beta on immutable releases;
4. earn full model-production sign-off prospectively;
5. add advanced optimization only after the core workflow is trusted and retained.

Until the prospective model-production gate passes, public outputs must say `Beta`,
`Model Preview`, or `Model xPts`. They must not claim independently verified accuracy, guaranteed
rank improvement, or a production-approved `AI Score`.

#### P0 — Role, minutes, and in-season decision safety

- [x] Distinguish missing previous-PL history from observed previous non-start history
      (`NO_PREVIOUS_PL_PLAYER_RATE_HISTORY` -- no row at all -- vs `NO_USABLE_PLAYER_RATE_HISTORY`
      plus `ZERO_LONG_FORM_MINUTES`/`ZERO_PRIOR_STARTS` -- a row exists but recorded no minutes;
      already flows through `baseline_pipeline.py`, coverage audit, and backtests); user-facing
      translation of these flags into plain-language warnings is now covered by `role_state`
      (below) and the named regression cases
- [x] Distinguish starter, substitute, unused substitute, not-in-squad, unavailable, and
      not-yet-eligible observations (`validation/appearance_observation.py`,
      `scripts/classify_appearance_observations.py`; retrospective classification of one
      completed, FINAL Gameweek's own outcome from data already in the database --
      `player_gameweek_stat` + the matching `availability_resolution_run` + `player_snapshot`
      registration + `fixture_snapshot` team participation. Caveat: FPL's live-data endpoint does
      not expose the 20-man matchday squad or bench list, so `unused substitute` and `not-in-squad`
      cannot actually be told apart from data available to this pipeline; rather than guess, both
      collapse into one honestly-named `unused_substitute_or_not_in_squad` bucket instead of
      claiming a distinction the source data cannot support)
- [x] Add a role state (`unknown`, `likely_starter`, `rotation`, `likely_bench`, `unavailable`)
      updated from official starts, minutes, availability, and reviewed evidence
      (`validation/role_state.py`; resolved eligibility takes precedence over a stale-high
      projection, and missing evidence reads `unknown` rather than being folded into
      `likely_bench`; wired into `recommend_lineup.py`'s output alongside `transparency`)
- [x] Detect material conflicts such as a 60+ minute start with low projected start probability,
      or a zero-minute available player with a high projected appearance probability
      (`validation/material_conflict.py`, `scripts/audit_material_conflicts.py`; retrospective
      only -- compares one completed model run against its own final event-live outcome and
      fails closed on a provisional event or a Gameweek/snapshot mismatch; the prospective
      counterpart that warns on a CURRENT decision is `decision/role_scenario_sensitivity.py`,
      the "sensitive decision" item further below)
- [x] Add named regression cases for Tzolis-like starters, current-only Fulham starters, and
      Henderson/Welbeck-like non-appearances (`tests/test_baseline_pipeline_regression_cases.py`,
      exercising the real `baseline_pipeline.py`/`appearance_pipeline.py` entry points, not
      re-derived logic: a Tzolis-shaped player -- zero previous-PL rate/appearance history but a
      real, priced, selectable squad member -- must be rescued by the empirical-prior cohort
      fallback with an explanatory flag rather than silently excluded or silently confident, mirroring
      the real Christos Tzolis reviewed in
      `docs/research/selected_squad_player_rate_evidence_2026_27.json`, whose actual fix is
      deliberately deferred pending a backtested external-league translation policy -- this test
      locks in the current, already-reviewed contract, not a claim the number is correct; a
      Fulham-shaped new signing with no appearance data anywhere must be excluded from
      `player_fixture_projection` entirely rather than defaulted, the opposite failure direction;
      and a reviewed appearance-scenario override must be able to override a strong
      history-derived projection for an established player carrying a genuine current-season
      doubt, the Henderson/Welbeck shape)
- [x] Expose simple `likely starter`, `rotation risk`, and `likely bench` presets on top of the
      existing reviewed appearance-scenario override boundary
      (`context/appearance_scenario_presets.py`, `--preset` on
      `scripts/add_appearance_scenario_override.py`; each preset's probability band deliberately
      reuses `role_state.py`'s own `ROTATION_THRESHOLD`/`LIKELY_STARTER_THRESHOLD` constants and
      `model/appearance.py`'s own default minutes, so a preset's name means the same thing a
      manager already sees in a player's role state rather than an independently drifting
      definition; individual `--start-if-available`/etc. flags still override one field of the
      chosen preset, and the result passes through the same `ConditionalAppearanceScenario`
      validation a fully hand-built scenario would)
- [x] Require source/reason, observation time, and one-deadline expiry for every override; preserve
      the immutable base projection beside the adjusted scenario (`context/availability.py`,
      `context/minutes.py`; `effective_until` is optional at create time but always resolved before
      storage -- defaults to the target Gameweek's own deadline when omitted, and a caller-supplied
      value beyond that deadline is rejected rather than silently clamped, so a reviewed override can
      never outlive the Gameweek it was reviewed for; the immutable base projection run is untouched
      by an override row, which only feeds a later, separate resolution/projection run. Caveat: the
      new `NOT NULL`/`CHECK` constraints on `availability_override.effective_until` and
      `appearance_scenario_override.effective_until` apply only to a freshly created database --
      DuckDB cannot `ALTER COLUMN` or add a `CHECK` constraint on a table that a foreign key still
      references, so there is deliberately no v14->v15 migration; an existing database must be
      re-initialised to pick up the DB-level constraint, though the Python-level validation in both
      `store_*` functions already enforces the same rule regardless of the underlying column's
      nullability -- see `docs/DATA_MODEL.md` and the comment above `SCHEMA_VERSION` in
      `storage/database.py`)
- [x] Compare base and plausible role scenarios; when the recommendation changes, label the
      decision `sensitive` and withhold an unconditional `Best option`
      (`decision/role_scenario_sensitivity.py`, wired into `scripts/recommend_lineup.py`'s
      `role_scenario_sensitivity` output field: for every squad member `role_state.py` already
      marks `ROTATION`, re-runs the same `recommend_lineup` search with that one player's
      projection replaced by a "blanks entirely" scenario -- 0 xPts, 0 appearance probability,
      exactly the 0-minute condition FPL's own autosub rule keys off -- one player at a time. The
      recommendation is labelled `sensitive`, and the specific player(s) responsible are named,
      when any such scenario changes the starting XI or captain; `LIKELY_STARTER`/`LIKELY_BENCH`/
      `UNAVAILABLE`/`UNKNOWN` players are never perturbed, since a plausible outcome for them would
      not plausibly change anything. This never alters the base recommendation itself -- only
      labels it and names the alternative that would flip it)
- [x] Materialize a deadline-safe current-season player-rate update from final official xG, xA,
      DefCon, and saves, with small-sample shrinkage and no retrospective post-match xP leakage
      (`model/current_season_rates.py`, `scripts/materialize_current_season_rates.py`; a NEW
      lineage -- `current_season_player_rate_run`/`current_season_player_rate` -- separate from
      `player_rate_history_run`/`player_rate_history`, which stay Vaastav-previous-season-only, so
      this is additive rather than a repurposing of that FK-constrained schema. Deadline safety:
      built only from `fpl_event_live_run` rows already FINAL -- `event_finished AND data_checked`
      -- as of the run's own `as_of`, so a provisional Gameweek, or the Gameweek being scored
      itself, can never leak into its own rate window. Shrinkage: each per-90 rate is blended
      toward that SAME player's own previous-season rate, weighted by `SHRINKAGE_PRIOR_MINUTES`
      (900.0, reusing `baseline_pipeline.py`'s own `PRIOR_REFERENCE_MINUTES`); a player with no
      previous-season history gets the raw current-season rate with an explicit
      `NO_PREVIOUS_SEASON_RATE_HISTORY` flag rather than an invented prior. Caveats: xGC is
      deliberately NOT a per-player field here -- it is a team-level input
      (`team_strength_projection`) in this model's architecture, never a per-player rate; cards/BPS
      are not yet included (only xG/xA/DefCon/saves, the components `_derive_rate_priors`'s own
      cohort fallback already covers); and, per an explicit scope decision, this run is
      MATERIALIZE-ONLY -- `baseline_pipeline.py`'s in-season path does not yet consume it, so
      `final_xpts` is unaffected and still comes entirely from the frozen previous-season
      `player_rate_history` (`FROZEN_PREVIOUS_SEASON_PLAYER_RATES`). Wiring this into production
      scoring is deliberately deferred to a separate, explicitly-approved change that should go
      through the existing shadow/calibration release path (`validation/release_health.py`) rather
      than changing `final_xpts` outright)
- [x] P0 sign-off: no owned-player lineup or transfer decision can depend on a material role
      conflict without a visible warning or reviewed scenario (verified by auditing every
      decision-producing CLI script: `recommend_lineup.py` already carried `role_state` and
      `role_scenario_sensitivity`; `recommend_transfers.py`, `plan_three_gameweeks.py`, and
      `optimize_initial_squad.py` carried `transparency` but not `role_state` -- this was the
      actual gap this sign-off check found, now closed by wiring `role_state`/`role_state_by_gameweek`
      into all three the same way `recommend_lineup.py` already does, each with a dedicated wiring
      test (`tests/test_recommend_transfers_role_state_wiring.py`,
      `tests/test_plan_three_gameweeks_role_state_wiring.py`,
      `tests/test_optimize_initial_squad_role_state_wiring.py`). Every owned-squad or transfer-target
      player on every decision surface now carries an explicit `unavailable`/`unknown`/
      `likely_bench`/`rotation`/`likely_starter` label; `recommend_lineup.py` additionally carries
      the richer `role_scenario_sensitivity` "would the recommendation itself flip" check, which the
      other three do not yet have -- extending that specific mechanism to transfer/rolling/initial-squad
      decision shapes is a distinct future enhancement, not required by this item's literal wording
      ("a visible warning **or** reviewed scenario"), which `role_state` alone already satisfies)

#### P1 — Usable three-menu MVP

- [x] Let a user load a public squad from an FPL Team ID without storing an FPL password
      (`GET /api/squad/from-entry/{id}` in `api/index.py`, backed by `ingest/fpl.py`'s
      `FPLClient.entry_picks` and `webapp/service.py`'s `resolve_entry_picks`; fetches from FPL's
      public `entry/{id}/event/{gw}/picks/` endpoint only -- never `my-team/{id}/`, which requires
      a login session -- and performs no server-side write, matching the app's existing
      "browser local storage only" boundary. The CLI/persistence equivalent,
      `ingest.squad_snapshot.import_squad_snapshot_from_entry`, exists separately for building an
      immutable `squad_snapshot` database row. Caveat: FPL's public payload carries no per-player
      purchase/selling price, so both are estimated from current market price and flagged
      `selling_price_is_estimated`, surfaced as a visible caveat in the UI rather than implying an
      FPL-exact sell value)
- [ ] Weekly squad: legal XI, captain, vice-captain, bench, raw xPts, marginal changes, and a small
      `Assumptions to review` surface only when a material owned-player conflict exists
- [ ] Three-Gameweek outlook: raw optimized-lineup xPts per GW and cumulative, player contribution,
      fixtures, captaincy, bench depth, and confidence; percentile rating is not an MVP blocker
- [ ] Transfers: rank hold and validated single-transfer alternatives, show no-hit first, and keep
      hit scenarios, multi-transfer, and chips explicitly separate
- [ ] Recompute all three menus from a reviewed role scenario without overwriting the base release
- [ ] Pin every response to an immutable release ID and expose status, capture time, freshness,
      finality, coverage, and sensitive-decision state
- [x] Add mobile-first interaction and end-to-end tests for Team ID to weekly decision without a CLI
      (`web/styles.css` mobile-first CSS -- `body { min-width: 1120px }` removed and replaced with
      `320px`; two new breakpoints at 860px and 480px collapse the sidebar into a horizontal
      scrollable top bar, stack the squad panel above content, and shrink every grid (pitch, bench,
      metrics, outlook, transfer cards) to fit a phone screen; verified with real Chromium
      screenshots at 320/390/820/1440px, zero horizontal overflow at any width.
      `tests/test_e2e_team_id_to_lineup.py` drives the actual served page in a real headless
      Chromium browser via a real `uvicorn` server run in a background thread -- not the FastAPI
      `TestClient`, which never touches app.js/the DOM -- covering Team ID entry, the resolved
      squad rendering as an 11-player legal XI with exactly one captain and one vice-captain badge,
      and the outlook/squad-editor menus. `FPLClient.entry_picks` is monkeypatched so no request
      reaches the real FPL API. New `playwright` dev dependency plus a one-time
      `playwright install chromium` step, documented in `docs/WEB_APP.md`)

#### P2 — Closed alpha and paid-founder beta

- [ ] Deploy the application and a separately operable projection/decision API with documented
      secrets, backups, rollback, monitoring, and cost limits
- [ ] Schedule and alert the complete snapshot-to-release refresh before every deadline
- [ ] Add authentication, entitlement, privacy/terms, account deletion, support, and Indonesian
      payment flow without storing FPL credentials
- [ ] Run a 10-20 user closed alpha across at least three deadlines; record conflicts,
      recommendation flips, failures, support load, and next-deadline return usage
- [ ] Open a clearly labelled founder beta only after the alpha gates pass; payment validates
      willingness to pay and does not imply that the model is production-approved
- [ ] Measure activation, retention, assumption-review completion, usefulness, and paid conversion
      before targeting 200 subscribers

#### P3 — Full production and post-MVP backlog

- [ ] Complete prospective 2026/27 appearance, xPts, calibration, uncertainty, and decision-policy
      evaluation, then promote only artifacts that pass overall and weakest-cohort gates
- [ ] Complete the versioned percentile rating contract below; raw xPts remains visible
- [ ] Complete operational sign-off for stale-data handling, incidents, rollback, privacy, support,
      and recommendation audit trails
- [ ] After retention is proven, resume multi-transfer/chip optimization, ownership/EO objectives,
      five-plus-GW branching plans, licensed enrichment, and editable non-minutes beliefs
- [ ] Treat any LLM as a presentation or structured-input layer, never as the projection or
      optimization source of truth

### Sprint 7 — Squad rating and application score contract

This full percentile contract belongs to P3. P1 may ship raw per-Gameweek and cumulative xPts with
confidence labels; until this contract passes, the UI should say `Model Preview` or `Model Score`,
not `AI Score`.

- [ ] Show raw optimized-XI-plus-captain xPts separately for GW, GW+1, and GW+2
- [ ] Define one fixed, reproducible benchmark population of legal same-budget squads for each
      frozen release; do not min-max against whichever scenarios happen to be open in the UI
- [ ] Define a per-Gameweek squad rating as a percentile against that release's benchmark
      population
- [ ] Define the overall three-Gameweek rating from cumulative optimized lineup xPts, not from the
      arithmetic mean of three rounded display ratings
- [ ] Keep model strength, data confidence, projection uncertainty, and squad-rule health as
      separate fields and badges
- [ ] Version the rating formula and persist benchmark identity, inputs, raw xPts, percentile, and
      explanation for reproducibility
- [ ] Validate monotonicity, stability across reruns, provisional-to-final drift, and sensitivity
      to captaincy, bench structure, injuries, and fixture changes
- [ ] Rating sign-off: values are comparable across all three menus and never conceal a failed
      projection-release or decision-policy gate

### Sprint 8 — Application experience: three primary menus

P1 implements the smallest safe subset of this application layer before the full percentile rating
contract. The application consumes immutable projection releases and decision outputs and does not
recompute modelling logic in the browser.

#### 8A — Shared application data contract

- [ ] Provide one application-facing release API/schema for projections, ratings, lineups,
      transfers, explanations, and quality gates
- [ ] Pin every response to one approved release ID and expose research/shadow/production status
- [ ] Keep all modelling, rating, and optimization calculations server-side; the browser only
      selects scenarios and presents persisted results
- [ ] Define consistent loading, stale-data, partial-coverage, provisional, and fail-closed states
      before building feature-specific screens

#### 8B — Weekly squad scenarios

- [ ] Compare the current squad with named alternative scenarios on an FPL-style pitch
- [ ] Show the model's legal best XI, captain, vice-captain, and ordered bench for the selected GW
- [ ] Show marginal xPts versus the current setup and transparent reasons for every change
- [ ] Surface coverage, freshness, provisional evidence, and uncertainty before any `Best option`
      label

#### 8C — Three-Gameweek squad rating

- [ ] Show GW, GW+1, and GW+2 rating cards plus raw expected lineup points for each Gameweek
- [ ] Show one overall three-Gameweek rating derived from cumulative expected points
- [ ] Let users inspect player-level contribution, fixture horizon, captaincy, bench depth, and
      risk without collapsing them into the rating
- [ ] Compare named squad scenarios against the same frozen release and benchmark population

#### 8D — Transfer recommendations

- [ ] Rank hold and transfer alternatives by the approved decision objective and three-Gameweek
      horizon, not by the cosmetic squad rating
- [ ] Show transfer cost, bank, free-transfer state, expected gain by GW, cumulative gain, and
      uncertainty
- [ ] Explain outgoing/incoming marginal value and show the best no-transfer and structural
      counterfactuals alongside the nominal recommendation
- [ ] Support only the transfer/chip scope that has passed Sprint 6 validation; label unsupported
      multi-transfer or chip paths explicitly

#### 8E — Application release hardening

- [ ] Add responsive/mobile interaction, accessibility, deterministic fixture snapshots, and
      end-to-end tests for all three menus
- [ ] Application sign-off: research/beta/production labels match the underlying release approval,
      and no menu can display a production recommendation from a failed or stale release

## License

Project code is released under the repository license. Third-party data remains subject to its respective source terms.
