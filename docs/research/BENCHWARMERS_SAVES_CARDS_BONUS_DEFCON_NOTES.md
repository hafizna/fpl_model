# Benchwarmers spreadsheet: saves / cards / bonus / DefCon extraction notes

Read-only research extraction from `MODEL.xlsx` (paths under
`C:\Users\hafizna.fadhli\Downloads\`). Machine-readable detail lives in
[`benchwarmers_saves_cards_bonus_defcon_reference.json`](./benchwarmers_saves_cards_bonus_defcon_reference.json);
this file is the narrative companion. **No workbook was modified or saved. No Python file
was written or modified as part of this extraction.** At extraction time, nothing in
`src/fpl_model/` implemented saves, cards, bonus, or DefCon logic.

## Scope actually inspected

`MODEL.xlsx`'s `MODEL`, `PPts`, `CONTROL`, `DC`, `FDRDC`, `PT`, `PSPT`, `API`, `PSAPI`, `PTS`,
`TABLES`, `TABLE`, `PSTABLE`, `MINS`, `PL` sheets. `SOLVER.xlsx` and `SIMPLE MODEL.xlsx`
contributed nothing, same as all three prior extractions.

## The exact numbered-component mapping

Determined by reading MODEL row-1 headers directly, not assumed:

| Column | Component | Extraction |
|---|---|---|
| 1 | Appearance | prior |
| 2 | 60+ minutes | prior |
| 3 | 3x Saves | **this extraction** |
| 4 | Yellow Cards | **this extraction** |
| 5 | Red Cards | **this extraction** |
| 6 | Bonus Points | **this extraction** |
| 7 | Assist | prior |
| 8 | Goal | prior |
| 9 | Clean Sheet | prior |
| 10 | 2+ Goals Conceded | prior |
| 11 | DC (DefCon) | **this extraction** |

**Penalty saves, penalty misses, and own goals are structurally absent.** `API!penalties_saved`,
`penalties_missed`, `own_goals` exist as raw data fields but are never referenced by any formula
in `MODEL` or `PPts` — confirmed by exhaustive search. These three real FPL scoring events are
simply not modelled anywhere in the 1–11 component chain.

## Saves flow

`MODEL!DA (PS %3) = PS_Saves_per_90 / 3` — the "1 point per 3 saves" rule modelled as a
**continuous rate**, not a discrete `floor(saves/3)` count, and with **no minutes-per-start
rescaling** (unlike cards' previous-season figure, which does rescale). `MODEL!CZ` gates this
to exactly 0 for every non-GK position. `MODEL!DC ('3')` blends previous/this-season (currently
100/0). `PPts!L` directly looks up `MODEL!3`, then `PPts!M` applies a **live fixture
adjustment** — the opponent's own attacking `xG/90` relative to league average (more shots
faced → more save chances) — reusing the same `VS xG/90`/`LA xG/90` columns from the
goals/assists chain. `PPts!N ('3')` is the product. Saves are **excluded** from `PPts!BG`
(the not-start branch) — a substitute earns exactly zero save xPts, joining clean sheets and
DefCon in that three-way exclusion.

## Cards flow

`MODEL!DG ('4', yellow) = blended_YC_rate * (-1)`; `MODEL!DK ('5', red) = blended_RC_rate * (-3)`.
**`PPts!O`/`P` are direct, unmultiplied lookups of `MODEL!4`/`MODEL!5` — cards are the only
component family across all four extractions with zero fixture/opponent adjustment of any
kind.** Cards **are included** in `PPts!BG` (scaled by the substitute-minutes ratio) — a
substitute does carry card risk, unlike saves/CS/DefCon.

**A real, verified bug: red cards are permanently disabled.** `CONTROL!B37=0` and `B38=0` — both
literal zeros, not a complementary `B38='=1-B37'` pair like every other weight in the workbook.
This forces `MODEL!5` to exactly `0.0` for every player, unconditionally, regardless of actual
red-card history. Whether this is deliberate ("too rare/noisy to model") or an authoring slip
cannot be determined from the spreadsheet alone.

## Bonus flow

`MODEL!DM (PS %6)`: for players with **more than 5** previous-season starts
(`CONTROL!B46`), blends their own historical Bonus/Start with a BPS/Start-implied rate; for
5-or-fewer-start players, uses **only** BPS/Start (a small-sample guard treating BPS as the
more reliable signal). `PPts!X` looks up `MODEL!6` directly, then `PPts!Y` applies **the exact
use of `PPts!V` requested by this task**: for GK/DEF, bonus is scaled by `VS xG/90/LA`
(opponent attacking strength, the inverted-around-1 transform already documented in the CS/GC
extraction); for MID/FWD, scaled by `VS xGC/90/LA` (opponent defensive weakness, a plain
ratio) — a genuine, deliberate-looking positional split. Bonus **is included** in `PPts!BG`.

**A dormant formula bug**: `MODEL!DO ('6')` = `(PS%6*B51)+(B52*%6)*BonusPts` parses, by
standard operator precedence, as `PS%6*B51 + (B52*%6*BonusPts)` — the `BonusPts` multiplier
binds only to the second term, unlike every structurally similar formula elsewhere (which wrap
the whole blended sum in one set of parens first). Currently invisible in cached output only
because `BonusPts=1` and `B52=0`. **No out-of-range (>3 or <0) bonus value was observed** in
the 7 sampled players, though no explicit clamp exists in the formula chain either.

## DefCon flow

The source is the FPL API's own single, pre-aggregated `defensive_contribution` count (via the
`DC` pivot sheet), **not** a hand-built sum of the separately-available
`clearances_blocks_interceptions`/`recoveries`/`tackles` fields — confirmed unused. `MODEL!CA
(LF DC/St)` rescales the raw rate by a blended `Mn/St%` (using `CONTROL!B89/B90`, a **distinct**
cell pair from `B62/B63` despite the near-identical label). `MODEL!CB (LF DC %) = 1 -
POISSON.DIST(threshold, lambda, TRUE)` — **a genuine discrete cumulative-distribution
probability of crossing a nonlinear threshold**, not a continuous-rate approximation (contrast
with saves). Threshold = **9 for DEF** (needs 10+), **11 for everyone else including GK** (the
IF-ladder has only two branches). `CONTROL!F198:F201` (DC points) = GK 0, DEF/MID/FWD 2 each —
GK is structurally excluded from ever scoring via the points constant, not via the threshold.

**DefCon has no live fixture adjustment at all**, despite a dead attempt (`PPts!AS/AT/AU`,
referencing an unresolved `DCSCORE` structured table and a `TABLES!E53:N53` league-average
block) existing with real cached values — confirmed never referenced by the live `AV`/`AX`
calculation. `PPts!AV = MODEL!11 / 2` — an unexplained division by 2, currently arithmetically
self-cancelling only because every scoring position's DC-points constant happens to equal 2
(see quirks). DefCon is **excluded** from `PPts!BG`, joining saves and clean sheets.

## Where appearance/start/minutes enter

Identical mechanism to every prior extraction, confirmed again by exhaustive search: none of
`MODEL!3/4/5/6/11` or their `PPts` derivatives reference `Chance of Playing`, `Start%`, or
`MODEL!2` anywhere. Probability enters exactly once, at the final `PPts!BA/BG/BI` blend. The
precise three-way split for `PPts!BG` (not-start branch) is now fully confirmed:

- **Always zero for non-starters**: saves (`3`), clean sheets (`9`), DefCon (`11`)
- **Scaled down but nonzero for non-starters**: cards (`4`,`5`), bonus (`6`), assist (`7`),
  goal (`8`), 2+GC (`10`)

## Nonlinear approximations

- **Saves**: FPL's "1 point per 3 saves" modelled as a **continuous rate** (`saves_per_90/3`),
  not a discrete integer-floor count.
- **DefCon**: modelled as a **genuine discrete threshold-crossing probability** via
  `POISSON.DIST`, the opposite approach from saves — the two closest-in-spirit "count-based
  threshold" rules in this workbook are handled with two different mathematical techniques.
- **Bonus**: no explicit 0–3 clamp exists anywhere in the traced chain; not observed to be
  breached in this sample, but structurally unguarded.

## Dead pipelines

Two more confirmed, extending the pattern from prior extractions:

1. **`FDRDC` sheet**: an entire team-and-tactical-role-by-GW pivot table (`ARS-CB`, `ARS-AM`,
   home/away split) — confirmed **entirely unreferenced** anywhere in `MODEL`/`PPts`/`TABLES`.
2. **`PTS` sheet**: a player-by-GW historical total-points pivot — confirmed **entirely
   unreferenced**; bonus instead uses `PSAPI`/`API`'s season-total `bonus`/`bps` fields directly.
3. **`PPts!AS/AT/AU`** (DefCon fixture attempt): computed, cached, never applied — see above.

## H/A behavior

No component-specific H/A logic exists anywhere in saves/cards/bonus/DefCon formulas
(confirmed by exhaustive search) — H/A enters only via the shared `BA`/`BG`/`BI` mechanism
already documented in all three prior extractions. The previously-documented double-application
quirk mechanically extends to cards and bonus (present in `BG`) but **cannot** affect saves or
DefCon (absent from `BG`), extending the same asymmetry already found for clean sheets.

## Promoted-team / missing-data behavior

**A new, structurally distinct mechanism from the CS/GC extraction's team-level lump-sum
pattern.** Dara O'Shea (Ipswich, promoted team) shows `LF DC Mins=0` and `LF DCs=0` genuinely
throughout the entire Long Form window — not concentrated in one column like the xGC lump-sum
pattern. Critically, `PT CODE ROW MATCH` resolves to a **valid row** (534) — this is not an
`IFERROR`-triggered fallback from a failed lookup. Confirmed **not team-wide**: a teammate
(Diop) shows real nonzero DefCon data despite fewer previous-season starts. This is best
characterised as a **provider coverage gap**: the appearance-tracking source (`PSPT`) evidently
covers this player's Championship-season history, while the Understat-derived `MINS`/`DC`
pivots evidently do not, for this specific player.

## Independent recomputation (all 7 required cases match to full float precision)

1. **Saves** (Dubravka): `1.3559230733411958` — exact.
2. **Cards** (Chiesa): `-0.5678233438485805` — exact.
3. **Bonus** (Haaland): `1.2109829831673722` — exact.
4. **Defender DefCon** (Senesi, via `scipy.stats.poisson`): `1.390755886767129` — exact.
5. **Midfielder DefCon** (Cook): `1.5975374509679099` vs cached `1.5975374509679097` — match to
   15 significant figures (float rounding noise only).
6. **Rotation/substitute full blend** (Yoro): `BG=0.12608918777255204`,
   `BI=2.682546889941271` — both exact, independently confirming saves/DefCon absence and
   cards/bonus presence in `BG`.
7. **Promoted-team/missing-data** (O'Shea): `AX=0.0` — exact; traced to genuinely zero
   `LF_DC_Mins`, not an `IFERROR` fallback.

## Likely bugs and unresolved ambiguities

- **Red cards permanently disabled** (`B37=0`, `B38=0` — deliberate choice or slip, undetermined).
- **Bonus formula's missing parenthesis** — dormant, would activate if `BonusPts≠1` or `B52≠0`.
- **DefCon's unexplained `/2`** in `PPts!AV` — currently self-cancelling only because every
  scoring position's DC-points constant equals 2; would silently break if that ever diverged.
- Why two separate `FORECAST.LINEAR`-style dead calibration/dampening attempts exist across the
  workbook (this extraction found a third, `DCSCORE`, alongside the two already known).
- Why DefCon's Short Form window is 10 GWs (`CONTROL!B84`) rather than the 6 used everywhere
  else — currently moot since `B86/B87=1/0` makes the Short Form window unused regardless.
- At least **13 independently-editable "Previous vs This Season" / "Long Form vs Short Form"**
  CONTROL weight pairs are now confirmed cumulatively across all four extractions, all currently
  either `1/0` or `0.8/0.2`, none linked.

## Lookahead audit

**No risk found.** Saves/cards/bonus have no rolling window at all (season-total based). DefCon's
window uses the identical lookahead-safe offset-ladder construction already verified in the
CS/GC extraction — the window's end always trails `CONTROL!E2` (current-GW pointer) by
construction, verified precisely for the current preseason snapshot.

## Verification performed this session

```
git diff --check
ruff check .
pytest -q
git status --short
```

Every formula string and CONTROL cached value recorded in the JSON was re-read from the live
workbook after writing and matched character-for-character / value-for-value.

No files under `src/fpl_model/`, `tests/`, `README.md`, `docs/DATA_MODEL.md`, or existing
research artifacts were modified. Files created:

- `docs/research/benchwarmers_saves_cards_bonus_defcon_reference.json`
- `docs/research/BENCHWARMERS_SAVES_CARDS_BONUS_DEFCON_NOTES.md` (this file)

## Post-extraction Python translation

The later implementation in `src/fpl_model/model/secondary.py` preserves the extracted workbook
values as named diagnostics and adds coherent scoring paths. In particular, saves use a Poisson
expectation for complete three-save bundles, red-card deductions are no longer disabled, bonus is
bounded to 0--3, and substitute exposure is separated from absence. DefCon retains the extracted
10/12 thresholds and two-point award, while its substitute branch recomputes threshold probability
for cameo minutes. Golden tests are in `tests/test_secondary.py`.

Penalty saves, penalty misses, and own goals remain explicitly unimplemented because the workbook
contains no baseline for them. Historical bonus/BPS is treated as a cross-regime prior rather than
evidence for the changed 2026/27 BPS rules.
