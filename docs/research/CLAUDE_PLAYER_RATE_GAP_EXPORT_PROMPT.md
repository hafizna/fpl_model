# Claude task: audit workbook coverage for missing GW1 player rates

Work in the local repository `C:\Users\hafizna.fadhli\fpl_model` and use
`C:\Users\hafizna.fadhli\Downloads\MODEL.xlsx` read-only. Do not modify, recalculate, save,
rename, or copy the workbook. Use `openpyxl` with `data_only=True` for cached values and a separate
read-only formula view where formulas are needed. Record the workbook size, modified time, and
SHA-256 before and after; they must be identical.

Read these files first:

- `README.md`
- `docs/DATA_MODEL.md`
- `docs/PIPELINE_ARCHITECTURE.md`
- `docs/research/preseason_baseline_gap_audit_2026_27.json`
- all four existing `BENCHWARMERS_*_NOTES.md` component references
- `src/fpl_model/storage/database.py`
- `src/fpl_model/model/baseline_pipeline.py`

The current complete baseline run is `baseline_9c8c8edce88e593d`. Query
`data/processed/fpl_model.duckdb` for every row in `baseline_projection_gap` carrying
`NO_PREVIOUS_PL_PLAYER_RATE_HISTORY`. There should be 128 player codes: 87 at promoted teams, 35
summer arrivals at established teams, and six returning/development players. Treat those counts as
validation targets, not assumptions to force if the live database differs.

Investigate whether the workbook contains usable cached previous-season player-rate inputs for
those exact player codes. Match by player code only. Do not use names as a runtime join. Resolve
columns by their live headers and formulas; do not guess Excel letters from memory.

For each gap player whose workbook row exists, export the raw cached inputs needed by the existing
Python rate schema, in exactly this order:

```text
player_code,player_name,position,season_minutes,season_starts,season_saves,
season_yellow_cards,season_red_cards,season_bonus,season_bps,
long_form_minutes,long_form_expected_goals,long_form_expected_assists,
short_form_minutes,short_form_expected_goals,short_form_expected_assists,
long_form_defcon_minutes,long_form_defensive_contribution,
short_form_defcon_minutes,short_form_defensive_contribution,source_coverage_flags
```

Write the CSV to
`data/raw/workbooks/benchwarmers_player_rate_gap_2026_27.csv`. It is gitignored. Use UTF-8 without
BOM. Do not convert a missing lookup or provider-coverage gap into an observed zero. Put a JSON
array in `source_coverage_flags` and distinguish, as far as the workbook evidence allows:

- workbook row absent;
- workbook row present but underlying rate-provider minutes absent;
- PSAPI lookup absent or ambiguous;
- genuine observed zero with positive source minutes;
- formula/cache error.

Use Dara O'Shea (`player_code=216616`) as a required coverage test. Existing research found real
appearance history but zero underlying xG/xA and DefCon minutes; preserve that as missing provider
coverage, not as evidence of zero attacking/DefCon ability. Do not manufacture Championship or
other-league translations.

Create exactly two tracked research companions:

- `docs/research/benchwarmers_player_rate_gap_2026_27_reference.json`
- `docs/research/BENCHWARMERS_PLAYER_RATE_GAP_2026_27_NOTES.md`

They must report:

- workbook identity before/after;
- the exact DB query and baseline lineage;
- all 128 requested player codes partitioned into workbook-row present/absent and usable/coverage-
  limited groups;
- CSV row count and SHA-256;
- per-field source header/formula map;
- O'Shea's exact audit;
- counts by promoted/summer-arrival/returning category;
- unresolved ambiguities without guessing;
- confirmation that no workbook, Python source, test, or existing documentation file was modified.

Re-read every exported cached value and formula mapping after writing. Run `git diff --check`,
`ruff check .`, and `pytest -q`. Do not commit. Report only the new files, hashes/counts, coverage
findings, verification results, and `git status --short`.
