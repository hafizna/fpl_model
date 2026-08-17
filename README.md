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
- explicit fixture/DGW composition and one-time home/away adjustment
- deadline-safe walk-forward fold and metric primitives

The next milestone is a reproducible historical baseline run before any context layer or ML is
enabled.

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
- [ ] Walk-forward backtesting
- [ ] Start-probability calibration
- [ ] xPts uncertainty
- [ ] Ablation tests for each contextual layer
- [ ] Squad/transfer optimizer

## License

Project code is released under the repository license. Third-party data remains subject to its respective source terms.
