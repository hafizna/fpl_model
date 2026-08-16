# Benchwarmers spreadsheet: clean-sheet / goals-conceded extraction notes

Read-only research extraction from `MODEL.xlsx` (paths under
`C:\Users\hafizna.fadhli\Downloads\`). Machine-readable detail lives in
[`benchwarmers_clean_sheets_goals_conceded_reference.json`](./benchwarmers_clean_sheets_goals_conceded_reference.json);
this file is the narrative companion. **No workbook was modified or saved. No Python file
was written or modified as part of this extraction.** The resulting golden cases are now consumed
by tests for [`defence.py`](../../src/fpl_model/model/defence.py), which preserves the live Poisson
rate path while making 60-minute eligibility and repeated goals-conceded deductions explicit.

## Scope actually inspected

`MODEL.xlsx`'s `MODEL`, `PPts`, `CONTROL`, `xGC`, `PL`, `TABLES`, `TABLE`, `PSTABLE` sheets,
plus structural checks of `CS` (confirmed unreferenced), `TEAMS` (confirmed blank and
unreferenced by this chain), `FIXTURES`, and `MINS`. `SOLVER.xlsx` and `SIMPLE MODEL.xlsx`
contributed nothing, same as both prior extractions.

## The exact clean-sheet calculation flow

**Two parallel pipelines exist, exactly like goals/assists. Only one is live**, and this time
the dead one is the entire `MODEL` sheet's CS/GC block (`BT:BX`, `CI:CM`, `DX:EE`) — confirmed
by exhaustive search that `MODEL!9`, `MODEL!10`, `MODEL!CS`, `MODEL!2+ GC` are never read by
anything downstream.

The **live** pipeline, entirely on `TABLES`/`PPts`:

1. **Team-level xGC rate.** `TABLES!R (LF xGC/90)` = team xGC summed over the 38-GW Long Form
   window (from the `xGC` pivot-table sheet, keyed by team abbreviation, not player code) /
   matches played over the same window. `TABLES!X (SF xGC/90)` mirrors this for the 6-GW Short
   Form window.
2. **LF/SF blend + Understat correction.** `TABLES!AD` blends LF/SF 80/20 (`CONTROL!B97/B98`).
   `TABLES!AF (LFxSF US xGC FIX)` then applies a 2-point linear regression correction
   (`CONTROL!B101:C102`, mapping raw xGC/90=2.0→×1.0, raw=0.9→×0.9) via `FORECAST.LINEAR`.
   **This is the figure `PPts!AK` reads** — the player's own team's corrected defensive rate.
3. **Opponent/league adjustment.** `PPts!AL (CS X)` = `AK * (opponent's LF/SF-blended
   attacking xG/90) / (league-average xG/90)`. This is the Poisson lambda.
4. **Poisson probability.** `PPts!AM (CS%) = EXP(-CS X)`. `PPts!AO ('9') = positional_CS_points
   * CS%`.

## The workbook goals-conceded calculation flow

Shares steps 1–3 above entirely (`AL`/`CS X` is common to both). Diverges at step 4:
`PPts!AP (% 2+ GC) = 1 - CS% * (1 + CS X)` — the Poisson `P(X≥2)` complement identity.
`PPts!AR ('10') = positional_2+GC_penalty * % 2+ GC`.

Post-extraction validation against the official 2026/27 scoring rules found an important semantic
gap: FPL deducts one point **for every** two goals conceded, so four goals conceded costs two points,
not one. The workbook models only the first `2+` event and is therefore an approximation, not an
exact implementation of the repeated deduction. The Python component retains this value for golden
parity and separately computes `E[floor(goals conceded / 2)]` for the coherent projection.

## Where start/appearance/60-minute probabilities enter

**Modelling question 4, answered directly:** `MODEL!2` (the 60-minute-conditional-on-start
probability) is used **directly**, but only on the dead `MODEL!BW`/`CL` formulas
(`EXP(-xGC*mins) * MODEL!2`). The **live** `PPts!AM (CS%)` formula and its entire upstream
chain (`AK`, `R`, `T`, and their sources) contain **zero references** to `MODEL!2`, `Chance of
Playing`, or `Start%` anywhere — confirmed by exhaustive formula-string search.

Start/appearance probability enters exactly once, at the same final `PPts!BA/BG/BI` blend
already documented for appearance and goals/assists, via `BH!Start%`. Nothing new here — but
one asymmetry is specific to this component family:

- **Clean sheets (`'9'`) are entirely excluded from `PPts!BG` (IF NOT START TOTAL)** — its
  formula sums only columns `4,5,6,7,8,10`. A substitute or non-starter's clean-sheet
  contribution to `TRUE TOTAL` is **exactly zero**, unconditionally.
- **Goals-conceded (`'10'`) IS included in `BG`**, scaled by the substitute-cameo-minutes
  ratio (`BF`) — a substitute *can* carry goals-conceded risk.

This directly answers modelling question 5: **no**, a substitute/below-60 player never
receives clean-sheet value, but does receive (scaled-down) goals-conceded exposure.

## Important quirks and likely bugs

1. **The entire MODEL-sheet CS/GC pipeline is dead.** Same authoring pattern found in the
   goals/assists extraction (parallel season-total assist columns), now applied to a whole
   component family. Notably, the dead pipeline is the *only* place `MODEL!2` gets applied —
   the live formula is structurally simpler (no minutes/60-minute scaling term at all, relying
   entirely on the `BA`/`BG` branch-exclusion mechanism instead).
2. **A second, independent dead-end calculation** exists in `TABLES` (`PPts x xG`/`xGC`,
   `Damp xG`/`xGC`) — a more sophisticated fixture-dampening attempt (different
   `FORECAST.LINEAR` calibration table, plus a sign-preserving soft-clip) that computes real
   numbers but is never referenced anywhere.
3. **`PPts!AK`'s `IFERROR` fallback is the string `"-"`, not `0`** — the sole exception to this
   workbook's otherwise-universal IFERROR-to-zero convention. A genuine team-lookup failure
   here would propagate `#VALUE!` errors downstream rather than silently zeroing out, a more
   dangerous (if more visible) failure mode. Not observed to occur for any of the 20 real teams.
4. **`CONTROL!B97/B98` has a dual role**: it blends both the player's own team's LF/SF
   defensive rate (via `TABLES!AD`) *and* the opponent's LF/SF attacking rate (via `PPts!R`,
   already documented for goals/assists) — one CONTROL cell pair silently coupling two
   conceptually distinct recency-weighting choices.
5. **Five separate 0.8/0.2-valued CONTROL pairs** now confirmed across the two extractions
   (`B59/B60` goals-assists, `B72/B73` clean sheets [dead], `B78/B79` 2+GC [dead], `B97/B98`
   fixture LF/SF), all currently identical, none linked.
6. **H/A-applied-twice** (documented previously) mechanically extends to goals-conceded (`'10'`)
   for non-starters, but — because clean sheets are absent from `BG` — **can never affect the
   clean-sheet component**, narrowing its footprint relative to goals/assists/cards/bonus/DC.

## Missing-data and promoted-team behavior

**A real, deliberate-looking fallback exists for the three promoted teams (Coventry, Hull,
Ipswich).** Their `xGC`/`PL` pivot-table rows have **zero in every one of the 37 real weekly
GW columns** of the previous-season window, with the **entire season's estimated total dumped
into a single final column** (`25-26-GW38`) — e.g. Coventry: 60.672 xGC, 38 "matches played"
in that one column alone. Established teams (including Sunderland, themselves promoted the
year before but with a full genuine PL season on record) have realistic per-GW spread across
all 37–38 columns.

Net effect: the resulting `LF xGC/90` rate (`lump_sum / 38`) is still a sensible season-average
figure — **not** a zero or an error — but `SF xGC/90` collapses to being **identical** to
`LF xGC/90` for promoted teams specifically (both windows' only nonzero column is the same
lump-sum column), so the LF/SF blend is a numerical no-op for these three teams even though
it's a genuine blend for everyone else. Independently verified for all three promoted teams by
inspecting the raw sheet rows directly, and for Ipswich specifically by hand-recomputing the
full Understat-correction step to full float precision.

No genuine zero-data/lookup-failure control case exists among real players for this component
family — the CS/GC join is purely team-level via a direct abbreviation match (`MATCH(TEAM,
...)`), and all 20 current-roster team abbreviations resolve cleanly in both the `xGC`/`PL`
sheets and `TABLES`. This absence is itself recorded rather than a case being fabricated.

## Lookahead audit

**No lookahead risk found.** The Long Form window for clean sheets/goals-conceded is
`CONTROL!E2 - E69` at its right edge (offset **40**, mapping precisely to `25-26-GW38`) —
**always one column short of `CONTROL!E2` itself** (offset 41, `26-27-GW00`, the current-season
marker). The offset formulas (`D69`/`E69` etc.) shift forward in lockstep with `CONTROL!B3` (the
"last GW played" marker) via an `IF`-ladder, structurally guaranteeing the window's end always
trails the current-GW pointer regardless of how far the season has progressed. Verified
precisely for the current `B3=0` snapshot; argued structurally (not independently re-verified
at other `B3` values) for the general case, per the JSON's `lookahead_audit.caveat`.

## Representative players extracted

Full inputs/formulas/cached values are in the JSON's `golden_cases` array (6 cases).

1. **Premium goalkeeper** — David Raya (ARS, GK). `CS%=0.504`, `clean_sheet_xpts=2.014`,
   `Start%=1` (isolates the pure eligible-branch value, same property as the appearance
   extraction's Raya case).
2. **Nailed elite defender** — Gabriel (ARS, DEF). **Identical** `CS%`/`AO`/`AR` to Raya (same
   team, same fixture, same CS/GC points since GK and DEF share identical position constants)
   — a clean sanity check confirming CS/GC is purely team-level, not player-specific.
3. **Rotation-prone defender** — Leny Yoro (MUN, DEF), 18 starts/14 subs previous season,
   `Start%=0.487`. Primary rotation/below-certain-start case: `TRUE TOTAL` independently
   recomputed end-to-end, directly proving column `9` is absent from `BG` while column `10`
   is present.
4. **Midfielder eligible for clean-sheet points** — Bukayo Saka (ARS, MID). Same underlying
   `CS%=0.504` as Raya/Gabriel, but MID's 1-point (not 4-point) CS constant and 0 (not -1)
   GC penalty — confirms the positional gate is a pure post-hoc multiplication.
5. **Forward control case** — Erling Haaland (MCI, FWD). `CS%=0.249` is a genuine, nonzero,
   non-trivial Poisson probability, but `AN`/`AQ` (positional points) are exactly 0 for FWD,
   forcing `AO`/`AR` to exactly `0.0` — proves the positional gate is a late multiplicative
   override, not an early exclusion from the probability calculation itself.
6. **Promoted-team defender** — Dara O'Shea (IPS, DEF). Primary promoted-team case: Ipswich's
   raw `LF xGC/90` exactly equals raw `SF xGC/90` (both `1.479448`), independently confirming
   the lump-sum data pattern. Elevated `CS X` (1.08) correctly reflects a genuinely weaker
   defensive prior, not a missing-data artifact.

## Independent recomputation (all 5 required cases match to full float precision)

1. **GK clean-sheet component** (Raya): `AL=(0.8304680655866142*1.26369)/1.5294868421052636
   =0.6861479032775635`; `AM=exp(-AL)=0.503511914736361`; `AO=4*AM=2.014047658945444` — exact.
2. **Defender clean-sheet component** (Gabriel): identical inputs/output to Raya — exact.
3. **Goals-conceded component** (Yoro): `AR=-0.15276233115996563` — exact.
4. **Rotation/below-certain-start case** (Yoro): full `TRUE TOTAL=2.682546889941271` recomputed
   end-to-end including an independent `BG` recomputation that explicitly excludes column 9 —
   exact.
5. **Promoted-team case** (O'Shea): `AO=1.3568557192124613`, `AR=-0.2940536446433706` — exact;
   the underlying Understat-correction regression step matched to 15 significant figures
   (a ~2×10⁻¹⁶ difference, pure float64 rounding noise).

## Unresolved ambiguities

- Why two separate `FORECAST.LINEAR` calibration tables exist (`B101:C102` live,
  `B105:C106` dead) with different calibration points and no explanatory comment.
- Why the `PL` sheet has two structurally different row blocks (`1001` used live via `TABLES`,
  `2200` used only by the dead `MODEL!BU/CJ`) — not cross-verified for consistency.
- Whether the promoted-team lump-sum figures are hand-entered Championship-to-PL translations,
  a formula-driven estimate, or a placeholder — confirmed as cached *data*, not a live formula,
  but the upstream derivation wasn't accessible from this read-only extraction.
- Whether the blank `TEAMS` sheet's state matters elsewhere in the workbook, outside this
  extraction's traced chain.

## Verification performed this session

```
git diff --check
ruff check .
pytest -q
git status --short
```

Every formula string recorded in the JSON was re-read from the live workbook after writing
and matched character-for-character (see the JSON re-verification pass this session).

No files under `src/fpl_model/`, `tests/`, `README.md`, `docs/DATA_MODEL.md`, or existing
research artifacts were modified. Files created:

- `docs/research/benchwarmers_clean_sheets_goals_conceded_reference.json`
- `docs/research/BENCHWARMERS_CLEAN_SHEETS_GOALS_CONCEDED_NOTES.md` (this file)
