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

## Current scope: baseline integration and validation

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

The next milestone is using the walk-forward backtest results to determine shrinkage and
uncertainty before any context layer or ML is enabled.

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

Audit whether historical Vaastav snapshots existed before each inferred deadline:

```bash
python scripts/audit_vaastav_snapshots.py --season 2025-26
```

Initialise the gitignored local snapshot database:

```bash
python scripts/init_local_db.py
```

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

### Sprint 1 — Data foundation
- [x] Repository/package structure
- [x] Official FPL API adapter
- [x] Vaastav adapter
- [x] Canonical schemas
- [x] Spatial fingerprint primitive
- [ ] Test structured preseason heatmap availability on Chelsea
- [ ] Canonical player ID bridge across providers

### Sprint 2 — Benchwarmers baseline
- [x] Reproduce appearance/start/minutes logic
- [x] Goal / assist components
- [x] Clean sheet / goals-conceded components
- [x] Saves / cards / bonus / DefCon
- [x] Fixture and home-away adjustments
- [x] Golden tests against spreadsheet outputs

### Sprint 3 — Context engine
- [ ] Promotion priors
- [ ] Manager regime features
- [ ] World Cup and preseason readiness
- [ ] Congestion / rest-day features
- [ ] Tactical role priors

### Sprint 4 — Validation and decisions
- [x] Walk-forward fold/metric primitives (smoke-tested)
- [x] Genuine 11-component walk-forward backtest (2025-26, in-season)
- [x] Segment diagnostics (position/gameweek/minutes/start-probability/xPts bands)
- [x] Paired gameweek-cluster bootstrap uncertainty of the model-vs-naive MAE/RMSE advantage
- [x] Walk-forward xPts calibration assessment (measurement only; see below)
- [ ] Apply xPts calibration to production projections
- [x] Walk-forward appearance-model (start-probability/expected-minutes) calibration assessment
      (measurement only; see below)
- [ ] Appearance-model shrinkage in production, if the assessment above supports it
- [ ] Per-player/per-fixture xPts uncertainty
- [ ] Ablation tests for each contextual layer
- [ ] Squad/transfer optimizer

## License

Project code is released under the repository license. Third-party data remains subject to its respective source terms.
