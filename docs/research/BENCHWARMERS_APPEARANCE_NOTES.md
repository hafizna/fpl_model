# Benchwarmers spreadsheet: appearance / minutes / 60-min extraction notes

Read-only research extraction from `MODEL.xlsx`, `SOLVER.xlsx`, and `SIMPLE MODEL.xlsx`
(paths under `C:\Users\hafizna.fadhli\Downloads\`). Machine-readable detail lives in
[`benchwarmers_appearance_reference.json`](./benchwarmers_appearance_reference.json);
this file is the narrative companion. **No workbook was modified or saved.** The extraction itself
did not change the Python implementation. The golden cases are now consumed by tests for
[`appearance.py`](../../src/fpl_model/model/appearance.py), whose translation preserves the useful
priors but deliberately excludes the wiring quirks documented below.

## Scope actually inspected

Only `MODEL.xlsx` contains the sheets relevant to appearance/minutes/60-minute logic
(`MODEL` and `PPts`, plus supporting `CONTROL`, `PREDICT`, `API`, `PT`, `PSPT`). `SOLVER.xlsx`
is a downstream squad optimizer and `SIMPLE MODEL.xlsx` is a derivative workbook without a
`MODEL`/`PPts` sheet — neither contributed formulas to this extraction.

## What the columns mean

### MODEL sheet, `CR:CY`

| Col | Header | Kind | Meaning |
|---|---|---|---|
| CR | `1-60 mins` | literal `1` | fixed multiplier, not a computed probability |
| CS | `PS %1` | formula | previous-season P(1-60 min appearance), from `PS UnSub/SqdsMd`, with a `0.01` floor when a player has zero previous-season starts and zero subs |
| CT | `%1` | formula | this-season mirror of CS; always hits its `0.01` fallback right now because this-season Starts/Subs are 0 during preseason |
| CU | `1` | formula | blended appearance probability: `(PS%1 * B8 + %1 * B9) * CR * Chance of Playing` |
| CV | `60+ mins` | literal `1` | fixed multiplier, mirrors CR |
| CW | `PS %2` | formula | previous-season P(60+ min \| started), `0.98` minus an interpolated sub-before-60 curve keyed on `PS Mn/St`, via a lookup table on `CONTROL!B13:C17` |
| CX | `%2` | formula | this-season mirror of CW, but the base constant is `0.99` (not `0.98` — see quirks) |
| CY | `2` | formula | blended 60+ probability: `(PS%2 * B19 + %2 * B20) * CV`. No `Chance of Playing` factor and no `IFERROR` wrapper, unlike CU |

`Chance of Playing`, `Starts`, `Subs`, `UnSub`, `Mn/St`, `Mn/Sub`, `St/SqdsMd`, `UnSub/SqdsMd`
(and their `PS`-prefixed previous-season counterparts) are documented field-by-field in the
JSON's `formula_map.MODEL_sheet.appearance_and_minutes_fields`. The short version: this-season
fields are name-matched lookups into `PT` (this season) / `PSPT` (previous season) via a 4-alias
dedupe pattern (`FPL_Name`, `web_name`, `FBRef_Name`, `Alt Name 2`), summed and then halved if
more than one alias matched.

### PPts sheet, `BA:BI`

This is where the per-fixture "start vs not-start" blend happens, one row per player per
upcoming gameweek/fixture.

| Col | Header | Meaning |
|---|---|---|
| BA | `IF START TOTAL` | sum of all 11 component xPts columns, home/away-adjusted once — "expected points assuming this player starts" |
| BB | `PSxTS Starts/Squads Made` | blended P(start \| in squad), reusing the `CONTROL!B8/B9` previous-vs-this-season weights against `MODEL[PS Sts/SqdsMd]` / `MODEL[St/SqdsMd]` |
| BC | `M Start %` | manual analyst override, looked up from `PREDICT!Manual Start %` by player code; blank if none set |
| BD | `MODEL 1` | direct lookup of `MODEL!'1'` (i.e. `CU`) for this player |
| BE | `T1` | **see quirk 1 below** — not simply `MODEL 1` when a manual override exists |
| BF | `PSxTS Mn/Sub / Mn/St` | ratio of blended sub-minutes to blended start-minutes, reused as a generic minutes-scaling factor |
| BG | `IF NOT START TOTAL` | sum of the *rate-scalable* components only (cards, bonus, assists, goals, DC — columns 4,5,6,7,8,10; explicitly excludes 60-min/saves/CS/DefCon-adjacent columns), scaled by BF, **already multiplied by H/A once** |
| BH | `Start %` | the actual probability used to weight BA vs BG in BI: manual override if present, else `Chance of Playing * BB` |
| BI | `TRUE TOTAL` | **see quirk 2 below** — final per-fixture expected points, but re-applies H/A on top of BG's already-adjusted value |

## Assumptions pulled from `CONTROL`

- **Preseason state**: `CONTROL!B3 = 0` ("LAST GW (Pre-Season = 0)"). The workbook, as extracted,
  has not yet played a current-season gameweek.
- **1-60 min blend**: `B8 = 1` (previous season), `B9 = 1-B8 = 0` (this season). The `'1'` column
  is currently **100% previous-season-weighted**.
- **60+ min blend**: `B19 = 1`, `B20 = 0`. Same story for the `'2'` column.
- Because this-season Starts/Subs/UnSub are all 0 for every player right now, `%1`/`%2` are stuck
  at their fallback constants (`0.01`/`0.49`) regardless of the nonzero `B9`/`B20` weights — they
  just don't contribute anything yet, numerically.
- **Mn/St → sub-before-60 lookup table** (`CONTROL!B13:C17`, used by CW/CX via `TREND()`):
  90→0%, 80→5%, 70→15%, 60→50%, 50→50%. Below 50, the flat 50% penalty applies without
  interpolation.

This is a meaningful caveat for anyone reading the golden cases: **every extracted number here is
a 100%-previous-season, preseason snapshot.** It does not demonstrate in-season blending behavior.

## Representative players extracted

Full inputs/formulas/cached values/outputs for each are in the JSON's `golden_cases` array.

1. **Certain starter** — David Raya (ARS, GK), 37/37 previous-season starts. `'1'=0.99`, `'2'=0.98`,
   `Start %=1` (so the not-start branch is weighted 0 and the H/A-double-application quirk is
   invisible for this player — useful as an isolation case).
2. **Rotation-prone starter** — Harvey Barnes (NEW, MID), 19 starts / 18 subs previous season
   (≈50/50). `'1'=0.964`, `'2'=0.92`, `Start %=0.5`. Hand-verified `TRUE TOTAL` to full float
   precision against the cached value — this is the case that exposes the H/A quirk cleanly.
3. **Frequent substitute** — Federico Chiesa (LIV, MID), 1 start / 25 subs / 10 unused-sub
   previous season. `'1'=0.715`, `'2'=0.48` (his `PS Mn/St=60` lands exactly on a `CONTROL`
   breakpoint, skipping `TREND()` interpolation — a nice edge-of-table check). `Start %=0.028`,
   so `TRUE TOTAL` is dominated by the not-start branch.
4. **Likely non-appearance** — Ben Wilson (COV, GK), 0 starts / 0 subs / 46 unused-sub previous
   season (permanent backup keeper). `'1'=0.01` (hits the zero-history floor), `Start %=0`, and
   `TRUE TOTAL` is **negative** (`-0.068`) — see quirk 3.
5. **Manual-start-override example** — Bukayo Saka (ARS, MID), included specifically because
   `PREDICT!Manual Start % = 0.6` is set for him, making the `T1` quirk directly verifiable by
   hand rather than trivially collapsing to `MODEL 1`.

All five cached-value sets were cross-checked: every formula string recorded in the JSON was
re-read from the live workbook and matched character-for-character, and the `TRUE TOTAL` formula
was independently recomputed from its cached inputs for Barnes and matched the cached output to
full floating-point precision.

## Spreadsheet quirks (flagged, not fixed)

1. **`T1`'s manual-start blend is not a probability-weighted average.** When `M Start %` is set,
   `T1 = 1*M_Start% + (1-M_Start%)*MODEL_1` — the manual value is added at full weight, not used
   to weight `MODEL_1` against some alternative. For Saka (`M_Start%=0.6`, `MODEL_1=0.9590625`),
   `T1=0.983625` — **greater than both inputs**. Meanwhile the actual start/not-start branch
   weight in `BI` comes from the separate `BH` (`Start %`) column, which *is* a clean 0–1
   probability. So the same manual input is used two different ways in the same row: once as a
   real probability (`BH`), once folded into a non-probabilistic point-boost term (`T1`).

2. **H/A (home/away multiplier) is applied twice to the non-start branch.** `BG` already bakes in
   one factor of `H/A`. `BI` then multiplies the *entire* blended total — including `BG` — by
   `H/A` again. Verified by hand on Barnes: `BG` without its internal `H/A` factor is
   `0.7371653883989414`; times `H/A=1.05` gives `0.7740236578188885`, matching the cached `BG`
   exactly. The start branch (`T1` and the `full_sum*Start%` term) is only ever `H/A`-adjusted
   once, via `BI`'s outer multiplication — so this asymmetry is specific to substitute-cameo
   expected points. This looks like an unintentional bug rather than a design choice.

3. **`TRUE TOTAL` can go negative** for a player who never plays. Wilson's `Start %=0` collapses
   `BI` to `BG*H/A` alone, and his rate-scalable component sum (cards/bonus/assists/goals/DC) is
   slightly negative before scaling, giving `TRUE TOTAL = -0.068`. The spreadsheet does not floor
   expected points at zero in this column.

4. **`PS %2` and `%2` use different base constants** (`0.98` vs `0.99`) despite otherwise-identical
   formula structure. Not explained anywhere in `CONTROL`; not parameterized. Left unresolved —
   see ambiguities below.

5. **The zero-history fallback for `PS %1`/`%1` is `0.01`, not `0`.** A player with truly zero
   previous-season starts and subs still gets a 1% appearance prior, not a hard zero. Deliberate
   Bayesian-style floor, but worth naming since it means the appearance column is never exactly
   zero for any available player.

6. **`CR` and `CV` are hardcoded literal `1`s**, not formulas, despite living in a row of
   formula-driven columns. They're currently no-ops as multipliers.

## Unresolved ambiguities

- **Why `0.98` vs `0.99`** for `PS %2` vs `%2` base constants (quirk 4) — no explanation found in
  `CONTROL` or elsewhere; flagged, not resolved.
- **Whether the `T1` manual-start formula (quirk 1) and the double H/A application (quirk 2) are
  intentional design choices or bugs** in the source spreadsheet — this extraction only documents
  behavior, it does not have access to the spreadsheet author's intent. Any Python reproduction
  will need an explicit decision on whether to replicate these exactly or treat them as defects to
  fix, since the task scope says the spreadsheet is "a reference for existing logic, not a
  requirement to copy every heuristic exactly."
- **The 4-alias name-matching dedupe** (`Starts1`, `Subs1`, etc.) was audited structurally but not
  verified row-by-row for every player — it's possible some player's aliases produce 3+
  simultaneous matches, which the dedupe formula's `/2` correction would silently miscount. Not
  observed in the 4 golden-case players, not ruled out in general.

## Verification performed this session

```
git diff --check   # no whitespace errors
ruff check .        # (see command output in session)
pytest               # (see command output in session)
```

No files under `src/fpl_model/` or `tests/` were modified. Files created:

- `docs/research/benchwarmers_appearance_reference.json`
- `docs/research/BENCHWARMERS_APPEARANCE_NOTES.md` (this file)
