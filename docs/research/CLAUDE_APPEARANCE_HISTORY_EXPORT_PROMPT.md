# Claude/Excel prompt: previous-season appearance-history export

Use the currently open `MODEL.xlsx` workbook only as a read-only source. Do not modify, recalculate,
save, or rename any workbook. Read the repository's `README.md`, `docs/DATA_MODEL.md`,
`docs/PIPELINE_ARCHITECTURE.md`, and
`docs/research/benchwarmers_appearance_reference.json` before extracting anything.

Export the resolved previous-season appearance inputs for every current player row in the workbook's
`MODEL` table to:

```text
data/raw/workbooks/benchwarmers_appearance_history_2025_26.csv
```

The CSV must be UTF-8 with exactly this header and order:

```text
player_code,player_name,starts,substitute_appearances,unused_substitute,minutes_per_start,minutes_per_substitute
```

Field mapping:

- `player_code`: the current player code used by the MODEL/API join;
- `player_name`: the workbook's current player display name;
- `starts`: resolved `PS Starts`, not this-season Starts;
- `substitute_appearances`: resolved `PS Subs`;
- `unused_substitute`: resolved `PS UnSub` (named in matchday squad but did not play);
- `minutes_per_start`: resolved `PS Mn/St`;
- `minutes_per_substitute`: resolved `PS Mn/Sub`.

Export underlying cached numeric values, not formula text or display-formatted strings. Do not
reconstruct values from Vaastav, names, or all zero-minute matches. Keep genuine zeros. Include
promoted-team and new-signing rows when they have a current player code. Reject duplicate or blank
codes rather than deduplicating by name.

Before handing off, verify these workbook reference cases against
`benchwarmers_appearance_reference.json`:

- Raya: 37 starts, 0 subs, 0 unused, 90 minutes/start;
- Barnes: 19 starts, 18 subs, 1 unused, 79 minutes/start, 26 minutes/sub;
- Chiesa: 1 start, 25 subs, 10 unused, 60 minutes/start, 11 minutes/sub;
- Wilson: 0 starts, 0 subs, 46 unused;
- Saka: confirm the complete exported row against the manual-start golden case.

Then run, without editing Python code:

```powershell
.venv\Scripts\python.exe scripts/import_appearance_history.py `
  --csv data/raw/workbooks/benchwarmers_appearance_history_2025_26.csv `
  --season 2025-26 `
  --source-label "MODEL.xlsx resolved previous-season appearance fields"
.venv\Scripts\python.exe scripts/project_preseason_appearance.py `
  --gameweek 1 --previous-season 2025-26
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest
```

Report the CSV row count and SHA-256, the five verified cases, import/projection run IDs, projected
versus missing player counts, Ruff/pytest results, and `git status --short`. The CSV and DuckDB are
gitignored runtime data: do not force-add them and do not commit anything.
