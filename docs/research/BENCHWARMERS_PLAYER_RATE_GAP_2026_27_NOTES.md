# Benchwarmers spreadsheet: player-rate coverage gap audit notes

Read-only research extraction from `MODEL.xlsx` (path under
`C:\Users\hafizna.fadhli\Downloads\`). Machine-readable detail lives in
[`benchwarmers_player_rate_gap_2026_27_reference.json`](./benchwarmers_player_rate_gap_2026_27_reference.json);
this file is the narrative companion. **No workbook was modified, recalculated, saved, renamed, or
copied. No Python source, test, or existing documentation file was modified.** This task is an
investigation into workbook coverage, not a formula-behavior audit like the five prior
`BENCHWARMERS_*_NOTES` extractions — it asks a yes/no question (does usable data exist?) for a
specific, already-identified set of 128 player codes, and answers it precisely.

## Scope and question

`docs/research/preseason_baseline_gap_audit_2026_27.json` records 128 current FPL players flagged
`NO_PREVIOUS_PL_PLAYER_RATE_HISTORY` by the GW1 baseline run `baseline_9c8c8edce88e593d`: 87 at
promoted teams, 35 summer arrivals at established Premier League teams, 6 returning or development
players. Re-querying `data/processed/fpl_model.duckdb` live this session against
`baseline_projection_gap` reproduced exactly 128/87/35/6 — the JSON's targets, not assumptions,
were confirmed against the live database before any workbook extraction began.

The question this task answers: **does `MODEL.xlsx` contain any usable cached previous-season
rate data for these 128 exact player codes**, matched strictly by `player_code`, never by name?

## The answer, in one line

**No player in the 128-player gap set has usable previous-season rate data in the workbook.**
Every one of the 128 codes falls into one of two entirely-missing states: 17 are absent from the
`MINS`/`xG`/`xA`/`DC`/`PL` windowed-rate sheets outright, and the other 111 have a row there but
every long-form and short-form figure resolves to exactly zero, while all 128 — with zero
exceptions — are absent from `PSAPI` (the previous-season season-totals table). This is a clean,
binary result, not a mixed picture with some usable and some unusable rows.

## Where player-rate data actually lives in the workbook

Two independent, code-keyed sources feed `MODEL`'s player-rate columns, both confirmed by reading
live formula text rather than assumed from a prior session:

1. **`PSAPI`** — a flat, previous-season (2025-26) player-season-totals table with a `code` column
   and `minutes`/`starts`/`saves`/`yellow_cards`/`red_cards`/`bonus`/`bps`/`expected_goals`/
   `expected_assists`/`defensive_contribution` fields (841 rows). `MODEL!PS Mins`/`PS Saves`/
   `PS YCs`/`PS RCs`/`PS Bonus`/`PS BPS` are each a direct `INDEX/MATCH` on `PSAPI[code]`, e.g.
   `=IFERROR(INDEX(PSAPI[minutes],MATCH(MODEL[[#This Row],[Code]],PSAPI[code],0)),0)`.
2. **A row-1001-1999 block on `MINS`/`xG`/`xA`/`DC`/`PL`** — a per-GW-delta pivot keyed by
   `player_code` in column A, with one column per `24-25-GW38` through `26-27-GW00` label.
   `MODEL!PT CODE ROW MATCH = MATCH(code, MINS!$A$1001:$A$1999, 0)` resolves each player's row
   index once; every windowed field (`LF G+A Mins`, `LF xG`, `LF xA`, `SF Mins`, `SF xG`, `SF xA`,
   `LF DCs`, `LF DC Mins`, `SF DCs`, `SF DC Mins`) then sums a `CONTROL`-anchored column range via
   `SUM(INDEX(sheet!$B$1001:$CZ$1999, row, start):INDEX(sheet!$B$1001:$CZ$1999, row, end))`.

These are genuinely separate data sources with separate lookup mechanisms — a player can be present
in one and absent from the other, and (as the O'Shea case proves) a "present" row in the windowed
block can still resolve to all zeros if the underlying provider has no minutes on record.

## Window resolution (re-derived, not assumed)

`CONTROL!E2 = 41` (current-GW pointer). Re-reading `CONTROL!B56:E56` (attacking long form),
`B57:E57` (attacking short form), `B83:E83` (DefCon long form), and `B84:E84` (DefCon short form)
this session and resolving the same `start_idx = E2-D+1-C`, `end_idx = E2-E` arithmetic already
proven in the clean-sheets/goals-conceded extraction's window audit gives:

| Window | Columns | GW labels | Matches `docs/DATA_MODEL.md` |
|---|---|---|---|
| Attacking/DefCon long form | D–AO | `25-26-GW01`–`25-26-GW38` (38 GWs) | Yes — "long form: GW1–38" |
| Attacking short form | AJ–AO | `25-26-GW33`–`25-26-GW38` (6 GWs) | Yes — "attacking short form: GW33–38" |
| DefCon short form | AF–AO | `25-26-GW29`–`25-26-GW38` (10 GWs) | Yes — "DefCon short form: GW29–38" |

The DefCon long-form window cells (`B83`/`C83`/`D83`/`E83`) are numerically identical to the
attacking long-form window cells (`B56`/`C56`/`D56`/`E56`), so `long_form_minutes` and
`long_form_defcon_minutes` are the same 38-column sum over the same `MINS` sheet — recorded as one
verified fact, not assumed, in the JSON.

**Sanity check before touching any gap player**: this exact window mechanism was independently
verified against David Raya (`player_code 154561`), whose LF/SF figures are already published from
a prior session. `long_form_minutes=3330`, `short_form_minutes=450`, `long_form_expected_goals=0`,
`long_form_expected_assists=0.07` — all four matched the live `MODEL` cache exactly before any of
the 128 gap-player codes were processed.

## Dara O'Shea (`player_code 216616`) — required audit case

O'Shea's `PT CODE ROW MATCH = 534` is a **valid** row index — his code genuinely exists at
`MINS!A1536` (and the equivalent row on `xG`/`xA`/`DC`/`PL`), not an `IFERROR`-triggered fallback
from a failed `MATCH`. Despite that, every windowed field is exactly zero: `long_form_minutes=0`,
`long_form_expected_goals=0`, `long_form_expected_assists=0`, `short_form_minutes=0`,
`long_form_defensive_contribution=0`, `short_form_defensive_contribution=0`, and both DefCon
minute windows are also zero. `PSAPI[code]=216616` is separately, independently absent (scanned
all 841 `PSAPI` rows — no match).

A third fact refines what the prior saves/cards/bonus/DefCon and goals/assists extractions already
recorded: `MODEL!'Previous Season Starts1'` (the field behind `MODEL!PS Starts=46`) is **not**
sourced from `PSAPI` at all — it is a **name-matched** lookup into `PSPT[Starts]`, tried against
four aliases (`FPL_Name`, `web_name`, `FBRef_Name`, `Alt Name 2`) in turn; only the `FPL_Name`
alias resolves for O'Shea, giving 46. Because this task requires a code-only join, this export does
**not** carry that 46 into `season_starts`. `season_starts` is left blank for O'Shea, consistent
with `PSAPI`'s own code-matched absence — a deliberate, documented choice, not an oversight. The CSV
therefore correctly preserves O'Shea's zero-attacking/zero-DefCon windowed figures and his blank
season totals as **missing provider coverage**, not as evidence of zero underlying ability, exactly
as the task requires.

## Coverage taxonomy applied to all 128 gap players

| Class | Count | Meaning |
|---|---|---|
| Workbook row absent (both `MINS`-block and `PSAPI`) | 17 | Code not found anywhere in the windowed-rate sheets; excluded from the CSV entirely. |
| Workbook row present, provider minutes absent | 111 | Row exists in `MINS`/`xG`/`xA`/`DC`/`PL`, but every long-form/short-form figure is exactly 0 — true for **all 111**, no exceptions. |
| PSAPI lookup absent or ambiguous | 111 | `PSAPI[code]` match fails — true for **all 128** gap players, but only 111 are written to the CSV (the other 17 are excluded on the row-absent rule above). `MATCH` is exact, so "ambiguous" never actually occurs: PSAPI has no duplicate codes. |
| Genuine observed zero with positive source minutes | 0 | Would require positive LF/SF minutes alongside a zero rate — no such case exists in this 128-player set. |
| Formula/cache error | 0 | An exhaustive scan of every relevant `PSAPI`/`MINS`/`xG`/`xA`/`DC` field for all 128 codes found zero Excel error strings — the workbook's universal `IFERROR`-to-zero convention (documented in every prior extraction) means a failed lookup surfaces as a clean `0`, not a propagated error. |

The 111-vs-111 overlap between "provider minutes absent" and "PSAPI absent" is not a coincidence to
resolve — it reflects that both counts describe the *same* 111 exported rows from two independent
angles (windowed-rate coverage and season-total coverage), and both angles agree: coverage is zero.

## Category counts

| Category | Total gap | Exported (row present) | Excluded (row absent) |
|---|---|---|---|
| Promoted team player | 87 | 80 | 7 |
| Summer arrival, existing PL team | 35 | 26 | 9 |
| Returning/development player | 6 | 5 | 1 (Madjo, AVL) |

All three totals match `docs/research/preseason_baseline_gap_audit_2026_27.json`'s stated targets
exactly, confirmed against a fresh DuckDB query this session rather than trusted from the prior
file alone.

## What was exported and why

The CSV writes one row per player whose code was found in the `MINS`/`xG`/`xA`/`DC`/`PL` row-1001
block (111 rows) — this is the row `MODEL!PT CODE ROW MATCH` itself would resolve against, so "the
workbook row exists" is defined the same way the live spreadsheet defines it. The 17 players
without even that row are excluded from the CSV rather than written with fabricated placeholder
values, per the task's explicit instruction not to convert a missing lookup into an observed zero.

Within the 111 exported rows:

- `long_form_*`/`short_form_*` fields are the workbook's own genuinely cached `0` values (a real,
  evidenced zero from a present row — not a substitute for a missing one), flagged
  `WINDOWED_RATE_PROVIDER_MINUTES_ZERO` / `WINDOWED_DEFCON_PROVIDER_MINUTES_ZERO`.
- `season_*` fields are **blank** wherever `PSAPI[code]` has no match (all 111 rows), flagged
  `PSAPI_LOOKUP_ABSENT`, rather than defaulting to the `0` that `MODEL`'s own `IFERROR` wrapper
  would produce internally. The CSV deliberately does not reproduce that internal fallback, since
  doing so would indistinguishably mix "genuinely zero" with "no data" for a field the task asks
  this extraction to keep apart.
- Every row also carries exactly one of `PROMOTED_TEAM_PLAYER`, `SUMMER_ARRIVAL_EXISTING_PL_TEAM`,
  or `RETURNING_OR_DEVELOPMENT_PLAYER`.

## Re-verification performed after writing the CSV

A completely fresh `openpyxl.load_workbook()` call (independent of the extraction script's
in-memory state) re-scanned `MINS`/`xG`/`xA`/`DC` and re-looked-up `PSAPI` for a 6-player sample —
the first, middle, and last exported rows, two further deterministic picks, and O'Shea specifically
— across all 10 numeric fields plus PSAPI presence. **Zero mismatches.**

## Workbook identity — unchanged

| | Before | After |
|---|---|---|
| Size (bytes) | 71300490 | 71300490 |
| Modified time | 2026-08-16T21:40:30.440420 | 2026-08-16T21:40:30.440420 |
| SHA-256 | `172715bcfd5086632a658a520b209423e6fa6b4e0c96cbdc12ad8010c7eb97a4` | `172715bcfd5086632a658a520b209423e6fa6b4e0c96cbdc12ad8010c7eb97a4` |

Identical on all three measures, and identical to every prior extraction session's recorded
identity for this same file.

## Unresolved ambiguities (not guessed)

- **Why 17 codes are absent from the `MINS` row-1001 block entirely while the other 111 at least
  have a present-but-zero row.** Both groups are current 2026-27 FPL players without previous-PL
  history; no FPL-side attribute checked this session (team, position, join date) obviously
  explains the split. Resolving it would require inspecting how the external, Understat-derived
  feed behind `MINS`/`xG`/`xA`/`DC`/`PL` is populated upstream of this workbook — out of scope for
  a read-only extraction.
- **Why `PSAPI` has zero matches for any of the 128 gap players.** Structurally expected (PSAPI is
  a previous-PL-season table; these are, by definition, players without previous-PL minutes), but
  this session did not independently verify PSAPI's exact population rule beyond observing that a
  genuine returning PL player (Raya) is present and all 128 gap players are absent.
- **Whether the `MINS`/`xG`/`xA`/`DC` pivot source could ever cover a promoted-team or Championship
  player under a different provider or a later refresh.** This extraction only records the current
  cached state, not the provider's coverage roadmap.
- **The relationship between O'Shea's name-matched 46 previous-season starts and his genuinely zero
  code-matched attacking/DefCon windows.** Both facts are real and independently confirmed; this
  extraction does not attempt to reconcile them into a single playing-time prior, since doing so
  would require exactly the name-based join this task asks to avoid.

## No safe promoted/new-player prior exists in this workbook

Per `docs/DATA_MODEL.md`'s player-fixture rate-history boundary, "a current player without linked
Premier League history remains missing pending an explicit promoted/new/returning-player prior."
This audit confirms the workbook itself supplies no such prior for any of these 128 players — the
next safe action is a reviewed, sourced prior-import boundary (Championship or other-league history
with an explicit translation policy), not automatic imputation from this workbook, and not a
Championship/other-league translation invented by this extraction.

## Verification performed this session

```
git diff --check
ruff check .
pytest -q
git status --short
```

No files under `src/fpl_model/`, `tests/`, `README.md`, `docs/DATA_MODEL.md`, or existing research
artifacts were modified. Files created:

- `data/raw/workbooks/benchwarmers_player_rate_gap_2026_27.csv` (gitignored)
- `docs/research/benchwarmers_player_rate_gap_2026_27_reference.json`
- `docs/research/BENCHWARMERS_PLAYER_RATE_GAP_2026_27_NOTES.md` (this file)
