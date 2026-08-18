# Claude Code prompt: export Benchwarmers preseason team strength

Work in the local repository `C:\Users\hafizna.fadhli\fpl_model`.

This is a read-only workbook extraction. You have filesystem/shell access, not an Excel add-in.
Use `openpyxl` to read cached values and formulas from:

`C:\Users\hafizna.fadhli\Downloads\MODEL.xlsx`

Do not recalculate, edit, save, rename, or copy over the workbook. Do not modify Python source,
tests, existing documentation, or the database schema. Do not commit anything.

Before extracting, read:

- `README.md`
- `docs/DATA_MODEL.md`
- `docs/PIPELINE_ARCHITECTURE.md`
- `docs/research/BENCHWARMERS_CLEAN_SHEETS_GOALS_CONCEDED_NOTES.md`
- `docs/research/BENCHWARMERS_FIXTURE_HOME_AWAY_NOTES.md`
- `docs/research/benchwarmers_clean_sheets_goals_conceded_reference.json`
- `docs/research/benchwarmers_fixture_home_away_reference.json`
- `src/fpl_model/ingest/team_strength.py`
- `scripts/import_team_strength.py`

## Required CSV

Create exactly:

`data/raw/workbooks/benchwarmers_team_strength_2026_27.csv`

Use UTF-8 without BOM and this exact header order:

```text
team_abbreviation,team_name,prior_type,long_form_matches,long_form_xg,long_form_xgc,short_form_matches,short_form_xg,short_form_xgc,league_average_xg_per_match,league_average_xgc_per_match
```

Export exactly these 20 current-team abbreviations, once each:

```text
ARS,AVL,BOU,BRE,BHA,CHE,COV,CRY,EVE,FUL,HUL,IPS,LEE,LIV,MCI,MUN,NEW,NFO,TOT,SUN
```

Locate the `TABLES` structured-table columns by their actual headers rather than assuming fixed
cell coordinates. For each team export the cached, resolved values corresponding to:

- `ABVR` -> `team_abbreviation`;
- current team name -> `team_name`;
- `LF PL` -> `long_form_matches`;
- `LF xG` -> `long_form_xg`;
- `LF xGC` -> `long_form_xgc`;
- `SF PL` -> `short_form_matches`;
- `SF xG` -> `short_form_xg`;
- `SF xGC` -> `short_form_xgc`.

Repeat the cached preseason league-average constants `PSTABLE!N24` and `PSTABLE!O24` on every
row as `league_average_xg_per_match` and `league_average_xgc_per_match`. Verify which metric each
cell represents from the sheet headers/formulas; do not infer solely from position.

Set `prior_type=promoted_team_prior` for exactly `COV`, `HUL`, and `IPS`. Set
`prior_type=observed_previous_pl` for the other 17 teams. This label describes provenance; it must
not change any workbook value.

Do not replace formulas with guessed values. Read formula text with `data_only=False` and cached
results with `data_only=True`. If the required cached results are missing, stop and report the
blocker rather than recalculating or inventing them.

## Required research reference

Create exactly one tracked companion file:

`docs/research/benchwarmers_team_strength_2026_27_reference.json`

Record:

- workbook absolute path, byte size, mtime, and SHA-256;
- extraction timestamp and read-only method;
- exact resolved header/cell mapping used for every CSV field;
- the relevant row-level formula strings from the first observed team and each promoted team;
- all 20 exported rows;
- CSV path, byte size, and SHA-256;
- validation results and warnings;
- unresolved provenance of the promoted-team lump-sum values, without guessing their origin.

Validate and record all of the following:

1. Exactly 20 rows, the exact abbreviation set above, no duplicates/blanks.
2. Exactly three promoted priors: COV, HUL, IPS.
3. All totals and match counts are numeric and non-negative; match counts are positive integers.
4. The two league-average values are positive and identical on every CSV row.
5. Recompute LF and SF rates as total/matches for every row.
6. For each promoted prior, verify LF rate equals SF rate separately for xG and xGC. This is an
   audit of the workbook's final-column lump-sum behavior, not a modelling assumption.
7. Recompute the 80/20 blends and the live xGC correction used by Python. Cross-check at least:
   - IPS LF xG/match = SF xG/match = `1.143972`;
   - IPS LF xGC/match = SF xGC/match = `1.479448`;
   - IPS corrected xGC/match = `1.4094362167912726`;
   - ARS corrected xGC/match = `0.8304680655866142`;
   - MUN corrected xGC/match = `1.2227514148689105`;
   - BOU blended xG/match = `1.7942807017543858`;
   - SUN blended xG/match = `1.1732105263157895`;
   - league-average xG/match = `1.5294868421052636`;
   - league-average xGC/match = `1.5294605263157894`.

Use normal floating-point tolerance for recomputation, but preserve the cached numeric values in
the CSV without rounding them for display.

## Import and verification

After writing the two files, run:

```powershell
.venv\Scripts\python.exe scripts/import_team_strength.py `
  --csv data/raw/workbooks/benchwarmers_team_strength_2026_27.csv `
  --target-season 2026-27 `
  --previous-season 2025-26 `
  --source-label "MODEL.xlsx TABLES resolved preseason team windows"
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q
git diff --check
git status --short
```

The importer must materialise exactly 20 teams and mark exactly three rows with
`PROMOTED_TEAM_PRIOR`. If it fails, do not edit the importer; report the exact validation error.

In your final response report the two created paths, both hashes, import/strength run IDs, row
counts, promoted-team flags, golden cross-check results, test results, and `git status --short`.
Confirm explicitly that the workbook and all Python files were untouched and nothing was committed.
