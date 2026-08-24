# Sprint 4 uncertainty, calibration, and penalty boundary

Sprint 4 engineering can run before the 2026/27 season is complete. Every evaluation fold still
requires its own outcomes to be final and available before the next deadline; prospective evidence
is accumulated one final Gameweek at a time.

## Historical residual uncertainty

`scripts/build_uncertainty_artifact.py` reruns the pinned 2025/26 Benchwarmers walk-forward
backtest and fits residual distributions at four fallback levels:

1. position + fixed xPts band + fixed start-probability band;
2. position + fixed xPts band;
3. position;
4. overall.

A segment is usable only with at least 100 rows across at least five Gameweeks. The 80% interval
uses the historical 10th and 90th percentiles of `actual_points - predicted_xpts`; predictive RMSE
is stored as the scalar dispersion measure. The walk-forward validation refits once per deadline
using only outcomes whose `outcome_available_at <= deadline`.

The committed 2025/26 result is
`docs/research/walk_forward_benchwarmers_2025_26_projection_uncertainty.json`:

- 14,566 walk-forward interval rows from 16,496 residual candidates;
- 78.47% overall empirical coverage for a nominal 80% interval;
- 75.23% FWD coverage, the weakest position result;
- mean interval width 4.60 xPts and mean predictive RMSE 2.42;
- empirical RMSE risk thresholds: low up to 2.416, medium up to 3.089, high above 3.089.

Because forward coverage is below target and the public backtest does not carry every current
production cohort, the artifact remains `shadow`. `scripts/apply_projection_uncertainty.py` writes
lower/upper xPts, risk band, segment scope, and lineage, but a shadow artifact does not populate
`player_fixture_projection.uncertainty`. Context/prior gaps such as promoted teams, current-only
appearance, empirical fallback priors, and position changes escalate risk by one band.

Premium, cheap-enabler, promoted-team, new/current-only, and position-change cohort labels are
implemented for prospective evaluation. They cannot be claimed validated until final 2026/27
outcomes exist for those exact production rows.

## Calibration shadow mode

`scripts/materialize_shadow_calibration.py` reads the committed 2025/26 xPts fit, stores an
immutable artifact, and writes counterfactual calibrated xPts beside the raw projection. It never
changes `final_xpts`, even if an artifact is labelled approved. This separates measurement from a
future scoring-policy decision and makes raw-versus-shadow MAE/RMSE available per cohort.

The historical fit is not automatically promoted because it was selected and measured on the same
season and its backtest team-strength source differs from the reviewed production prior. Production
activation requires a prospectively frozen confirmatory verdict.

## Penalty and non-penalty xG

Official FPL event-live rows expose total expected goals but do not identify whether an attempt was
a penalty. The model must not infer penalty xG from a player's total.

`scripts/add_penalty_review.py` therefore accepts a complete reviewed penalty ledger for one final
event-live run. Once the ledger is complete, every player receives an auditable decomposition:

```text
total expected goals = penalty expected goals + non-penalty expected goals
```

A header-only CSV is a valid complete review asserting that the Gameweek contained no penalty attempts.
Without a completed review, no decomposition row is created. A penalty taker's reviewed penalty xG
cannot exceed total xG and reviewed penalty goals cannot exceed official goals.

This storage boundary is ready for a future current-season attacking-rate refresh. It does not yet
alter the frozen previous-season production rate, so a late penalty cannot silently inflate
open-play ability.

## Activation gates

Mean calibration and scalar uncertainty remain inactive until all applicable gates pass:

- final, `data_checked` prospective outcomes;
- a frozen policy and artifact identity before evaluation;
- acceptable overall and cohort-specific coverage;
- separate premium, enabler, promoted, new-signing, and position-change diagnostics;
- paired Gameweek-clustered MAE/RMSE evidence for calibration;
- no material failure in the weakest supported segment.

The system can collect these results after every final Gameweek; it does not need to wait for GW38.
