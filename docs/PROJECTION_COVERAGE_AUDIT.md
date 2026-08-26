# Projection coverage audit

The coverage audit classifies every row already withheld by
`baseline_projection_gap`. It is a diagnostic and prioritisation layer only: it never fabricates a
rate, appearance probability, or xPts value.

Run it against an explicit immutable baseline:

```bash
python scripts/audit_projection_coverage.py \
  --model-run baseline_... \
  --json-output outputs/projection_coverage_audit.json \
  --csv-output outputs/projection_coverage_gaps.csv
```

The audit requires a player-identity bridge tied to the baseline's official FPL snapshot. It
assigns one `primary_reason` to every gap using this precedence:

1. roster blocked;
2. missing both appearance and player rate;
3. missing appearance only;
4. previous-PL row exists but has no usable minutes/starts;
5. promoted current-only player has no previous-PL rate;
6. other current-only player has no previous-PL rate;
7. missing previous-PL rate not explained by the identity bridge;
8. another projection input is missing.

Identity, rate, appearance, roster, position, and cheap-enabler cohorts remain separate dimensions.
This avoids treating "cheap", "promoted", or "new" as mutually exclusive root causes.

The Sprint 3 gate is at least 95% coverage among selectable players and 100% coverage for every
optimizer shortlist and selected squad. Until both conditions hold, the recommender remains
`RESEARCH_ONLY`.

The original v6 audit found 404 projections, 196 gaps, and 68.1% selectable coverage. After the
auditable empirical priors in baseline policy v7, the same frozen inputs produce 583 projections
and 17 gaps. All remaining gaps are roster-blocked, so selectable coverage is 100% and the coverage
gate passes. This does not waive calibration, uncertainty, or squad-economics gates.

## The 100% shortlist/squad half of the gate

The 95% selectable-player half above is checked by `audit_projection_coverage.py`. The 100% half
was, until Sprint 5, only ever stated here in prose: `decision/lineup_store.py` already fails
closed if any of a manager's 15 owned players lacks a projection, but
`decision/transfer_store.py`, `decision/rolling_store.py`, and `decision/initial_squad_store.py`
only ever silently excluded missing-projection candidates from their optimizer shortlists and
counted them as `excluded_missing_projection` -- underrepresenting missing/new/cheap players in the
choice set exactly as the initial-squad diagnostic below describes, without ever failing on it.

`validation/decision_coverage.py` turns those existing counts into a checked verdict.
`recommend_lineup.py`, `recommend_transfers.py`, `plan_three_gameweeks.py`, and
`optimize_initial_squad.py` each attach a `coverage_gate` object to their JSON output: one
`owned_squad` pool (100% required; expected to always pass since `load_lineup_inputs` already
enforces it upstream, but still checked explicitly rather than assumed) plus one `shortlist` pool
per Gameweek pool the command actually searched. `coverage_gate.passes` is `false`, and
`failing_pools` names every pool below 100%, whenever any candidate was excluded for a missing
projection -- the same underlying condition that produced the Watkins/goalkeeper-structure failures
below. This does not change what any command computes; it only makes the existing exclusion counts
into an explicit, reportable gate instead of an unread number.
