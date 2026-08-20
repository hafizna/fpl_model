# Frozen Appearance Calibration Confirmatory Protocol — 2026–27

**Status:** Frozen upon commit
**Protocol version:** `appearance_global_calibration_confirmatory_2026_27_v1`
**Policy implementation source:** commit `2c2c57d`
**Season:** 2026–27
**Burn-in period:** GW1–GW5
**Confirmatory evaluation window:** GW6–GW15
**Primary comparison:** `global` versus `raw`

## Purpose

This protocol prospectively tests whether the global appearance-calibration policy that performed best in the exploratory 2025–26 analysis also improves FPL expected-points predictions during a future, previously unseen evaluation window.

The 2025–26 result was exploratory because the candidate policies were selected after inspecting that season. The 2026–27 result defined here is confirmatory only for observations from GW6 through GW15, provided this document is committed before the GW6 deadline and remains unchanged afterward.

GW1–GW5 are a predefined burn-in period. They may supply causal training data but must not contribute to the confirmatory error metrics.

## Frozen Research Question

On identical player-fixture rows from GW6–GW15 of 2026–27, does the frozen global appearance-calibration policy improve both MAE and RMSE relative to the uncalibrated raw policy?

No other appearance-calibration policy is part of this confirmatory test.

In particular:

- `high_end_shrinkage` is excluded.
- No alternative threshold may be introduced.
- No new calibration functional form may be substituted.
- No subgroup result may replace the primary full-cohort result.

## Policies

### Raw control

`raw` uses the causal `AppearanceProjection` produced by the existing model without applying an appearance-calibration transformation.

### Global calibration policy

For target gameweek \(G\), fit the following ordinary least-squares calibration using eligible historical rows:

\[
actual\_started = \alpha_G + \beta_G \times raw\_start\_probability
\]

The calibrated start probability for a target player-fixture is:

\[
p_{start}^{calibrated}
=
clip(\alpha_G + \beta_G p_{start}^{raw}, 0, 1)
\]

The fit is recalculated independently at every gameweek. The fitted slope and intercept are therefore allowed to change as new causal training outcomes become available. The algorithm and constants defined in this document are not allowed to change.

## Training Eligibility

A historical row may enter the calibration fit for target gameweek \(G\) only if both conditions hold:

1. `row.gameweek < G`; and
2. `row.outcome_available_at <= target_deadline`.

The availability timestamp is:

```text
outcome_available_at = fixture_kickoff + 3 hours
```

The inferred target deadline is:

```text
target_deadline = earliest gameweek kickoff - 90 minutes
```

A fixture carrying an earlier gameweek label but played after the target deadline must not enter the fit until its outcome is actually available.

The fit requires at least five distinct eligible historical gameweeks after applying the availability filter:

```text
DEFAULT_MINIMUM_CALIBRATION_GAMEWEEKS = 5
```

If no valid fit exists—including insufficient gameweeks or a degenerate predictor—the global policy must fall back to the raw projection for that fold. No pooled, future-informed, or fabricated fit is permitted.

## Appearance Projection Reconstruction

This policy is not a pure replacement of `start_probability`. After applying the OLS transformation, the dependent appearance fields are proportionally reconstructed using:

```text
ratio = calibrated_start_probability / raw_start_probability
```

For rows where `raw_start_probability > 0`:

```text
calibrated_substitute_probability =
    clamp(
        raw_substitute_probability * ratio,
        lower=0,
        upper=1 - calibrated_start_probability,
    )

calibrated_appearance_probability =
    calibrated_start_probability
    + calibrated_substitute_probability

calibrated_sixty_minute_probability =
    clamp(
        raw_sixty_minute_probability * ratio,
        lower=0,
        upper=calibrated_appearance_probability,
    )

calibrated_expected_minutes =
    calibrated_start_probability * mean_minutes_per_start
    + calibrated_substitute_probability * mean_minutes_per_substitute
```

The appearance and 60-minute xPts fields are recomputed using the existing appearance-model arithmetic.

For `raw_start_probability == 0`, the raw projection is retained because the proportional ratio is undefined.

No independent `expected_minutes` calibration is fitted.

The reconstructed projection is passed through the existing, unchanged component-weighting and `compose_baseline_projection` scoring chain.

## Frozen Model and Analysis Constants

The following constants are fixed:

```text
minimum_calibration_gameweeks = 5
deadline_buffer = 90 minutes
outcome_delay = 3 hours
short_form_gameweeks = 6
defcon_short_form_gameweeks = 10
long_form_weight = 0.8
bootstrap_resamples = 10,000
bootstrap_seed = 42
bootstrap_cluster = gameweek
confidence_interval = percentile 95% interval
required_confirmatory_gameweek_clusters = 10
```

Changing any of these values creates a new policy or protocol version and must not amend this confirmatory result.

## Evaluation Cohort

Calibration history must be materialized beginning with GW1 so that GW1–GW5 can provide burn-in data.

Only scored player-fixture observations from GW6–GW15 may contribute to the confirmatory metrics.

The global and raw policies must be evaluated on exactly the same `(player_id, fixture_id, gameweek)` keys. Any row-set mismatch invalidates the run.

Existing causal gap and missing-data rules remain unchanged. Values must not be fabricated to improve coverage.

The archived source revision, import run ID, model commit, protocol commit, evaluated row count, gap count, and evaluated gameweeks must be recorded in the result artifact.

## Primary Metrics

The primary metrics are paired:

- Mean absolute error
- Root mean squared error

For both metrics, improvement is defined as:

```text
raw_error - global_error
```

A positive value therefore favors global calibration.

Uncertainty must use the existing paired gameweek-cluster percentile bootstrap with 10,000 resamples and seed 42.

## Predefined Verdict

The result is classified as follows:

**Confirms**
Both the MAE-improvement and RMSE-improvement 95% confidence intervals lie entirely above zero.

**Does not replicate**
Both confidence intervals lie entirely below zero.

**Ambiguous**
Any other result, including:

- either interval crossing zero;
- MAE and RMSE disagreeing in direction; or
- insufficient eligible evaluation clusters.

A confirming or non-replicating verdict requires paired scored observations from all ten gameweek clusters in GW6–GW15. If any required gameweek has no paired scored observation, the verdict is ambiguous and must identify the missing cluster or clusters.

An ambiguous result must remain ambiguous. The evaluation window must not be extended, shortened, or selectively redefined after inspecting the result.

A later checkpoint requires a separately frozen protocol committed before that checkpoint's additional outcomes are inspected.

## Checkpoint and Reporting

The single confirmatory checkpoint is GW15.

The analysis runs after all required GW6–GW15 outcomes are available in the pinned archive. It must not report a confirmatory verdict before GW15.

The result must be published regardless of whether it confirms, fails to replicate, or is ambiguous.

Secondary position, probability-band, team, or gameweek-phase analyses may be reported only as exploratory diagnostics. They must not replace the primary verdict.

## Change Control

Once committed, this document must not be edited.

Any change to:

- the tested policy;
- its formula or reconstruction rule;
- constants;
- training eligibility;
- burn-in or evaluation window;
- cohort construction;
- metrics;
- bootstrap method; or
- verdict criteria

requires a new protocol version in a new document. It must not be presented as an amendment to this protocol.

Corrections to implementation bugs must preserve the original artifact and be reported transparently as a separate version or sensitivity analysis.

## Guard Requirements

The confirmatory wrapper must verify that:

1. this document is tracked and clean;
2. its frozen Git commit is an ancestor of the run commit;
3. its contents match the blob stored in the frozen commit;
4. the frozen commit predates the GW6 evaluation deadline;
5. the policy implementation matches the frozen source commit or an explicitly verified field-equivalent implementation;
6. training history begins at GW1 while confirmatory metrics include only GW6–GW15; and
7. only `global_vs_raw` is emitted as the confirmatory comparison.

The guard must reject a protocol committed after the first confirmatory deadline. It must not reject a protocol because it predates archived data—that is the intended condition.

Git history provides an auditable paper trail, although commit timestamps alone are not absolute protection against rewritten local history. Preserving the freeze commit in a non-rewritten remote or tag strengthens the audit trail.

## Production Decision

A confirming result permits a production-adoption discussion; it does not automatically modify the production model.

A non-replicating or ambiguous result does not permit switching post hoc to `high_end_shrinkage`, another threshold, or another calibration formula within this confirmatory analysis.
