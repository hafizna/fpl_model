# Benchwarmers spreadsheet: goal / assist expected-points extraction notes

Read-only research extraction from `MODEL.xlsx` (paths under
`C:\Users\hafizna.fadhli\Downloads\`). Machine-readable detail lives in
[`benchwarmers_goals_assists_reference.json`](./benchwarmers_goals_assists_reference.json);
this file is the narrative companion. **No workbook was modified or saved. No Python file
was written or modified as part of this extraction.** The resulting golden cases are now consumed
by tests for [`attacking.py`](../../src/fpl_model/model/attacking.py), which preserves the live rate
path while keeping the spreadsheet quirks below out of the coherent projection layer.

## Scope actually inspected

`MODEL.xlsx`'s `MODEL`, `PPts`, `CONTROL`, `xG`, `xA`, `PL`, `MINS`, `TABLES` sheets, plus
structural checks of `PT`, `PSPT`, `PSAPI`, `API` (the raw appearance/assist-count sources).
`SOLVER.xlsx` and `SIMPLE MODEL.xlsx` contain no `MODEL`/`PPts` sheets and contributed
nothing here, same as the appearance extraction. Goals conceded, clean sheets, saves,
cards, bonus, and DefCon were out of scope except where needed to show they're excluded
from a shared formula (`PPts!BG`).

## The exact goal/assist calculation flow

**Two parallel pipelines exist. Only one is live.**

MODEL has a full second computation of assists (`PS A`/`PS A/St` from previous-season raw
assist counts, `A`/`A/St` from this-season raw assist counts, plus the xA equivalents
`PS xA/St`, `xA/St`) that is **never read by anything downstream** — confirmed by an
exhaustive formula-string search across both sheets. Don't be misled by these columns; they
compute real numbers but nothing consumes them.

The **live** pipeline, in order:

1. **Windowed xG/xA sums.** `MODEL!LF xG`/`LF xA` sum a player's expected-goals/expected-
   assists over a "Long Form" rolling window (`CONTROL!B56` = 38 GWs) from the dedicated `xG`/
   `xA` pivot-table sheets, matched by `PT CODE ROW MATCH` (a row-index lookup into `MINS!A`).
   `SF xG`/`SF xA` do the same over a "Short Form" window (`CONTROL!B57` = 6 GWs). During
   preseason, both windows resolve entirely to previous-season data (the long form is
   effectively last season in full; the short form is the last 6 GWs of last season).
2. **Rate conversion.** `LF xG/St` = `(LF xG / LF G+A Mins) * 90 * blended_Mn/St%`. This
   answers **modelling question 1** precisely: the denominator is *minutes actually played in
   the window* (giving a per-90-played rate), which is then **rescaled** by a separate blended
   minutes-per-start factor to approximate a per-start basis. It is not a pure per-90, per-
   appearance, or per-minute figure — it's a per-90-played rate multiplied by a start-minutes
   fraction. `SF xG/St`/`SF xA/St` mirror this for the short-form window.
3. **LF/SF blend, converted to points.** `MODEL!8` (goal) = `(LF xG/St * 0.8 + SF xG/St * 0.2)
   * positional_goal_points`. `MODEL!7` (assist) = `(LF xA/St * 0.8 + SF xA/St * 0.2) * 3`.
   These MODEL-sheet columns are **already points-valued**, not raw rates — worth remembering
   since PPts immediately divides the points back out (see quirks).
4. **Fixture adjustment (PPts sheet).** `PPts!AG` recovers the pure xG/St rate
   (`MODEL!8 / MODEL!Goal`), multiplies by `VS xGC/90/LA` (the upcoming opponent's
   goals-conceded rate relative to league average — a weaker defence inflates this), then
   `PPts!AJ` re-applies the positional points multiplier. Assists follow the identical shape
   (`AB` → `AC` → `AD` → `AF`), with one addition: `PPts!AD` applies a flat **+40% "Fantasy
   Assist Boost"** (`CONTROL!B65 = 0.4`) that has no goal-side equivalent.
5. **Start/not-start blend.** Only at this final stage does start probability enter — see
   below.

## Where expected minutes and start probability enter — answering questions 2 and 3

**Modelling question 3, answered directly:** appearance/start probability is **never present**
inside `MODEL!7`, `MODEL!8`, or any of `PPts!AB`/`AC`/`AD`/`AF`/`AG`/`AH`/`AJ`. Verified by an
exhaustive search of those formulas' text for `"Chance"` and `"Start"` — zero matches. The goal
and assist *rate* is computed completely independently of how likely the player is to play.

**Modelling question 2, answered directly:** start probability and expected minutes enter
exactly once, at the very last step, in `PPts!BI (TRUE TOTAL)`:

- `PPts!BA (IF START TOTAL)` sums all 11 components (including `'7'`/`'8'`) at full value,
  H/A-adjusted once — "if this player starts, these are the goal/assist points on offer."
- `PPts!BG (IF NOT START TOTAL)` sums only the rate-scalable components (`4,5,6,7,8,10` —
  goals and assists ARE included here) and multiplies by `PPts!BF`, the ratio of blended
  expected substitute-cameo minutes to blended expected starting minutes
  (`MODEL!G+A PSxTS Mn/Sub / MODEL!G+A PSxTS Mn/Start`). This is how a substitute's goal/assist
  expectation gets scaled down for reduced minutes.
- `PPts!BH (Start %)` — the manual override if set, else `Chance of Playing * PSxTS
  Starts/Squads Made` — is the actual probability that blends `BA` and `BG` inside `BI`.

So: goal/assist rates are computed as if-you-play rates first; probability and minutes-scaling
are applied only in the final blend, and only there.

## CONTROL assumptions identified

All still reflect the preseason snapshot (`CONTROL!B3 = 0`) documented in the appearance
extraction. New assumptions specific to goals/assists:

| Cells | Label | Values | Effect |
|---|---|---|---|
| `B59`/`B60` | Long Form / Short Form weight (goals & assists) | 0.8 / 0.2 | Blends the player's own LF and SF xG/St, xA/St rates. **Not** a preseason 100/0 split — both windows draw from previous-season data right now, but the 0.8/0.2 blend itself is a real, general-purpose weighting, unlike the appearance block's degenerate 1/0. |
| `B62`/`B63` | Mins/Start Previous vs This Season (goals & assists) | 1 / 0 | A **separate** knob from the appearance block's `B8`/`B9`, currently numerically identical but independently editable — see quirks. |
| `B65` | Fantasy Assist Boost | 0.4 | Flat +40% multiplier on the fixture-adjusted assist rate only. No goal-side equivalent. |
| `B97`/`B98` | Fixture LF/SF weight (opponent xG/xGC) | 0.8 / 0.2 | Blends the *opponent's* own LF/SF attacking and defensive rates — a third, separately-editable 0.8/0.2 pair. |
| `B110`/`B111` | League-average xG/xGC Previous vs This Season | 1 / 0 | Blends the league-average denominator used to express opponent strength relative to average. |
| `C198:C201` | Positional points per goal | GK=10, DEF=6, MID=5, FWD=4 | Looked up via nested `IF`, not `INDEX`/`MATCH`, despite the table's tabular layout. |
| — | Points per assist | 3 (literal, both `MODEL!DP` and `PPts!AE`) | Hardcoded twice, not sourced from `CONTROL`. |

## Representative players extracted

Full inputs/formulas/cached values are in the JSON's `golden_cases` array (6 cases — one more
than the minimum four profile categories, because the manual-override case and the
high-output-midfielder case both needed dedicated players).

1. **Elite high-xG forward** — Erling Haaland (MCI, FWD). `LF xG/St = 0.74` dominates his
   `goal_xpts_if_start = 2.84`; assist rate comparatively low (`0.09` xA/St), correctly
   reflecting a poacher profile. `Start % = 0.944`.
2. **High-output midfielder** — Cole Palmer (CHE, MID), chosen over Saka since Saka is reused
   for the manual-override case. Strong on both axes: `goal_xpts_if_start = 2.12`,
   `assist_xpts_if_start = 0.42`, away fixture (`H/A = 0.95`).
3. **Manual-start-override midfielder** — Bukayo Saka (ARS, MID), `PREDICT!Manual Start % =
   0.6`. Materiality check confirms the override changes `BH!Start%` (0.6 vs the
   historical-rate-derived 0.78125) and therefore the *weighting* between Saka's start-branch
   and not-start-branch goal/assist contributions in `TRUE TOTAL` — but it does **not** touch
   `AF`/`AJ` (the rate components) themselves, and Saka's `T1` quirk (documented in the
   appearance extraction) plays no role in the goal/assist columns at all.
4. **Attacking defender** — Gabriel (ARS, DEF). Nonzero, meaningful xG (`LF xG/St = 0.096`)
   that's worth disproportionately more per chance because of the DEF 6-points-per-goal
   multiplier. `goal_xpts_if_start = 0.70`.
5. **Rotation-prone substitute** — Federico Chiesa (LIV, MID). `Start % = 0.028` — his
   individually large goal/assist rates (`AJ=1.24`, `AF=0.39`) barely register in the
   start-weighted term; nearly all of his `TRUE TOTAL = 0.993` comes from the not-start branch,
   scaled by `BF = 0.183` (cameo-to-start minutes ratio).
6. **Zero-attacking-output control** — Dara O'Shea (IPS, DEF), 46 previous-season starts but
   `LF G+A Mins = 0` and all xG/xA windowed sums = 0, so `MODEL!7 = MODEL!8 = 0` and both goal
   and assist xPts are exactly 0. See quirks — this may be a genuine zero or a data-matching
   gap, and the extraction can't tell which from the MODEL/PPts sheets alone.

## Independent recomputation (all match to full float precision)

1. **Goal component** — Haaland: `(MODEL!8 / MODEL!Goal) * VS_xGC_90_LA * MODEL!Goal =
   (2.9665818595025777/4) * 0.9575214285509455 * 4 = 2.8405657000242286`, exact match to cache.
2. **Assist component** — Gabriel: `(MODEL!7/3) * VS_xGC_90_LA * 1.4 * 3 =
   (0.14727272727272728/3) * 1.0439184094839085 * 1.4 * 3 = 0.21523699570086402`, exact match.
3. **Rotation/substitute case** — Chiesa's full `TRUE TOTAL`: independently recomputed both
   `BG` (`0.27427072571066713`) and `BI` (`0.9934717177137987`) from cached inputs; both match
   the workbook cache exactly. This confirms the H/A-double-application mechanism (documented
   for appearance) mechanically carries through to goals and assists whenever `Start % < 1`,
   since both are among `BG`'s summed columns.

## Important quirks and likely bugs

1. **Two dead-end assist/xA computations** (`PS A`, `PS A/St`, `PS xA`, `PS xA/St`, `A`,
   `A/St`, `xA`, `xA/St`) exist in MODEL but are never consumed by anything. The live pipeline
   runs entirely through `LF`/`SF xG`/`xA`, not through raw API assist counts. Not a bug (no
   cached number is affected), but a real trap for anyone editing `CONTROL` weights expecting
   them to touch "the assist model" — they might be touching a column nothing reads.
2. **A third independently-editable Previous/This-Season weight pair** (`B62`/`B63`,
   "Mins/Start") exists alongside the appearance block's `B8`/`B9` and the DefCon block's own
   pair — currently all `1`/`0` and easy to mistake for one shared knob, but they are not
   linked in the spreadsheet.
3. **Fantasy Assist Boost (+40%) has no goal-side counterpart.** Likely intentional (a
   deliberate correction for xA underestimating real assists) but the magnitude `0.4` is
   unexplained anywhere in `CONTROL`.
4. **Redundant multiply-then-divide round trip.** `MODEL!7`/`8` are pre-scaled to points, then
   `PPts!AB`/`AG` immediately divide the scaling back out. Mathematically inert within the
   traced chain (confirmed by the exact-match recomputations), most likely because `MODEL!7`/`8`
   are meant to be read directly as a human-facing points column elsewhere in the workbook.
   Not confirmed against an actual consuming formula — flagged as probable-benign, not certain.
5. **`V` and `W` (opponent-relative-to-league-average) use different transformation shapes** —
   `V` is inverted (`1 + (1 - ratio)`), `W` is a plain ratio. Goals and assists use `W`
   exclusively, so this doesn't affect their correctness, but it means a shared "fixture
   adjustment" helper function would need two branches, not one formula.
6. **O'Shea's zero output may be a data gap, not a genuine zero rate.** 46 real previous-season
   starts but `LF G+A Mins = 0` in the MINS pivot table — either his data provider genuinely
   has no minutes on record (plausible for a Championship player, if that source doesn't cover
   the second tier) or `PT CODE ROW MATCH` failed to find his row and `IFERROR` silently floored
   the sum to 0. Not resolved by this extraction; would need inspecting `MINS!A` directly.
7. **The H/A-double-application quirk (documented for appearance) has real magnitude for
   goals/assists specifically.** For a heavy-rotation attacking player like Chiesa, goals+assists
   (`0.39 + 1.24 = 1.63`) are the dominant share of `BG`'s rate-scalable sum (`1.57` total, since
   cards/bonus/DC are small or negative for him) — so the double H/A application isn't a minor
   footnote for rotation-prone attackers, it's overstating a meaningful fraction of their
   expected points.

## Unresolved ambiguities

- Why `V` and `W` use different transformation shapes (inverted vs plain ratio) — no comment or
  `CONTROL` documentation found.
- Why `B65`'s Fantasy Assist Boost has no goal-side equivalent, and why `0.4` specifically.
- Whether `MODEL!7`/`8`'s points-scaled form is consumed elsewhere in the workbook as a
  standalone display value (a chart, a different sheet not audited here) — not verified either
  way.
- Whether O'Shea's zero windowed minutes reflect genuine zero Championship-tier data coverage
  or a silent `PT CODE ROW MATCH` lookup failure — requires inspecting the `MINS` sheet's raw
  player-code index directly, out of scope for this pass.
- Why the positional goal-points lookup uses nested `IF` rather than `INDEX`/`MATCH` against
  `CONTROL!B197:D201`, which is laid out as a proper table — purely a style question, both
  approaches verified to produce identical cached results for all 4 positions in the golden
  cases.

## Verification performed this session

```
git diff --check
ruff check .
pytest -q
git status --short
```

No files under `src/fpl_model/`, `tests/`, `README.md`, or existing documentation were
modified. Files created:

- `docs/research/benchwarmers_goals_assists_reference.json`
- `docs/research/BENCHWARMERS_GOALS_ASSISTS_NOTES.md` (this file)
