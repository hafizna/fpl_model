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
