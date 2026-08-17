# Benchwarmers spreadsheet: fixture-selection and home/away extraction notes

Read-only research extraction from `MODEL.xlsx` (paths under
`C:\Users\hafizna.fadhli\Downloads\`). Machine-readable detail lives in
[`benchwarmers_fixture_home_away_reference.json`](./benchwarmers_fixture_home_away_reference.json);
this file is the narrative companion. **No workbook was modified or saved. No Python file
was written or modified as part of this extraction.** This task was research-only.

## Scope actually inspected

`MODEL.xlsx`'s `PPts`, `FIXTURES`, `PREDICT`, `CONTROL`, `TABLES`, `TEAMS`, `MODEL` sheets.
`SOLVER.xlsx` and `SIMPLE MODEL.xlsx` contributed nothing, consistent with all four prior
extractions. This file builds directly on the appearance, goals/assists, clean-sheets/goals-
conceded, and saves/cards/bonus/DefCon extractions' documentation of `PPts!BA/BG/BI` and each
component's fixture multiplier — every shared formula string was re-read fresh this session and
confirmed byte-identical to what those four files already recorded.

## Fixture discovery and selection — the exact mechanism

`PPts!B` (`GW`) and `PPts!C` (`GW2`) are **pure row-position formulas**
(`CEILING(A2,2000)/2000` and an `ISEVEN` test), entirely independent of `CONTROL!B3`/`E2`. Every
player gets exactly **two fixture slots per gameweek**, materialized across all **38 gameweeks
unconditionally** — a fixed 76,000-row grid (38 GWs × 2 slots × 1000 players), computed
regardless of how far the season has actually progressed. This is a structurally different
mechanism from every historical-window formula documented in the prior four extractions (which
anchor to `CONTROL!E2`) — the "current GW" pointer only controls where each component's
*historical lookback* window ends, never which future fixtures get a row.

**Opponent discovery**: `PPts!Q` (`VS`) concatenates the player's team with a `GW3` slot key
(e.g. `"ARS1-1"`) and looks it up against `FIXTURES!FIXHELPER`'s `HOMEABRVCODE` (home fixture,
returns the away opponent uppercase) or falls back to `AWAYABRVCODE` (away fixture, returns the
home opponent lowercase). No match at all → the literal string `"-"` (a blank/bye slot).

**Home/away representation**: purely by **letter case** of `VS`. `PPts!AZ` (`H/A`) tests
`EXACT(VS, UPPER(VS))` — home → `1.05`, away → `0.95`.

**The double-gameweek detector**: `FIXTURES!FIXHELPER!HOMEABRVCODE`/`AWAYABRVCODE` build a
`"<team><GW>"` key, then append `-1` or `-2` via a **running `COUNTIF`** (from the top of the
fixture list down to and including the current row) that counts prior occurrences of that same
team-GW pairing. First occurrence → `-1`; any repeat → `-2`. This is exactly what feeds `PPts`'s
`GW2=1`/`GW2=2` slot split.

**Manual overrides**: the only fixture-*selection*-adjacent manual lever found is
`PREDICT!BB2:BC2` (literal cells, currently `1` and `10`) — a hand-set GW range that bounds the
dashboard's `SUMIFS` aggregation window. This affects *aggregation*, not fixture identity. The
already-documented per-player `Manual Start %` override (`PREDICT!H`) does not touch fixture
selection at all.

## Double gameweeks: independently scored, not collapsed

**Confirmed directly, not inferred.** Every gameweek's two slots are fully independent `BA`/`BG`/
`BI` evaluations, each with its own `VS` lookup and its own `H/A`. `PREDICT!D`/`E`
(`IF START PTS`/`TRUE PTS`) use `SUMIFS(..., PPts[GW], ">="&BB2, PPts[GW], "<="&BC2)` — since
both DGW slots share the same `GW` value, they are summed together automatically, **not**
collapsed into a single row anywhere upstream.

**The workbook's only apparent DGW is a fixture-data anomaly, not a genuine calendar
double-gameweek.** Ipswich Town and Nottingham Forest both show two fixtures tagged `event=2`
(GW2) in the raw `FIXTURES` data — but one of them (fixture id 369) has `kickoff_time
='2027-05-23'`, nearly a season later than every other GW2 fixture. This is a rearranged/
postponed fixture retaining a stale `event` tag, not a real fixture pile-up. The same two teams
show a genuine **blank at GW37** (zero fixtures), consistent with this being that displaced
fixture's original "true" slot. Verified directly on O'Shea's rows: GW2 slot 1 (vs Man Utd,
away, `TRUE TOTAL=2.226`) and GW2 slot 2 (vs Nott'm Forest, away, `TRUE TOTAL=2.890`) are both
real, independently-computed, nonzero projections; GW37 both slots show `VS='-'`, `BA=0`,
`BI=0`. A `SUMIFS`-style consumer would sum GW2 to `5.116` — a genuine combined double-fixture
total from two structurally separate calculations, exactly as the mechanism is designed to
produce.

**Blank fixtures/byes**: `PPts!J`, `BA`, and `BI` each independently guard on `LEN(VS)<3`,
zeroing the row. `PPts!BG` has **no equivalent guard of its own** — it computes a nonzero value
from whatever's in columns 4/5/6/7/8/10 even for a blank row (confirmed: O'Shea's GW37 rows show
`BG=0.1236` despite `VS='-'`) — but this is harmless in the current chain since `BI`'s own guard
forces the final `TRUE TOTAL` to `0` regardless of `BG`'s value.

## Fixture-strength calculations — complete source-to-xPts trace, per component

Six components carry a live fixture adjustment; two do not (confirmed by exhaustive
formula-string search across every `PPts` row-2 formula for `VS`, `H/A`, `FIXHELPER`):

| Component | Fixture signal | Formula |
|---|---|---|
| Saves (`3`) | Opponent's own attacking `xG/90` ÷ league average | `PPts!M = VS xG/90 / LA xG/90` |
| Goals (`8`) | Opponent's defensive weakness ÷ league average | `PPts!AH = LFxSF xG/St * VS xGC/90/LA` |
| Assists (`7`) | Same as goals, plus a separate +40% boost | `PPts!AC = LFxSF xA/St * VS xGC/90/LA` |
| Clean sheets & 2+GC (`9`,`10`) | Own team's xGC × opponent's attacking `xG` ÷ league average | `PPts!AL (CS X) = (own xGC * VS xG/90) / LA xG/90` |
| Bonus (`6`) | **Position-dependent**: GK/DEF use `V` (opponent attack, inverted), MID/FWD use `W` (opponent defense weakness, plain ratio) | `PPts!Y` |
| **Cards (`4`,`5`)** | **NONE** — direct `MODEL!4`/`MODEL!5` lookup, no multiplier at all | `PPts!O`/`P` |
| **DefCon (`11`)** | **NONE live** — a real, cached position-specific attempt (`PPts!AS/AT/AU`, referencing an unresolved `DCSCORE` table) exists but is never referenced by the live `AV`/`AX` calculation | `PPts!AV = MODEL!11/2` |

No other live fixture multiplier was found anywhere in `PPts` or `MODEL`.

## Home/away adjustment — full trace and the double-application proof

**It is one global scalar** — not team-specific, not position-specific, not component-specific.
Exactly two values (`1.05` home, `0.95` away), computed once per `PPts` row from `VS`'s letter
case, applied identically to every player and every component.

**It adjusts component-level *totals*, never the underlying event rates.** None of
`MODEL!3/4/5/6/7/8/9/10/11` or their `PPts`-side fixture-adjusted rate columns reference `H/A`
at all — confirmed by exhaustive search. `H/A` enters at exactly three points:

1. `PPts!BA` = `PRE_H_A_TOTAL * H/A` — one multiplication, the full 11-column sum.
2. `PPts!BG` = `(cols 4,5,6,7,8,10) * BF * H/A` — one multiplication, only the six
   not-start-eligible rate-scalable columns.
3. `PPts!BI` = `(T1 + full_sum*Start% + BG*(1-Start%)) * H/A` — the **entire** blended
   expression, **including the already-`H/A`-scaled `BG` term**, multiplied by `H/A` again.

**Double application: proven, not merely observed.** Expanding `BI`'s not-start contribution:
`BG*(1-Start%)*H/A = rate_sum*BF*H/A*(1-Start%)*H/A = rate_sum*BF*(1-Start%)*H/A²`. Every other
term in the *same* `BI` expression — `T1` and `full_sum*Start%` — carries only `H/A¹`. This is a
direct algebraic consequence of `BG`'s own formula already containing one `H/A` factor before
`BI`'s outer formula wraps it in a second. Verified by hand-recomputation matching cached `BG`/
`BI` to full float precision for three golden cases (home, away, and a manual-override home
fixture), and by comparing each against a hypothetical single-`H/A` variant:

| Case | H/A | 1−Start% | Cached BI | Hypothetical single-H/A BI | Difference |
|---|---|---|---|---|---|
| Raya (certain starter) | 1.05 | 0 | 4.840471 | 4.840471 | **0** (vanishes) |
| Yoro (away, rotation) | 0.95 | 0.5135 | 2.682547 | 2.685784 | −0.003237 |
| Gabriel (home) | 1.05 | 0.0625 | 6.641352 | 6.638337 | +0.003015 |
| Saka (home, manual override) | 1.05 | 0.4 | 4.674895 | 4.651025 | **+0.023870** |

**Components affected**: cards (`4`,`5`), bonus (`6`), assist (`7`), goal (`8`), 2+GC (`10`) —
exactly the six columns present inside `BG`. **Structurally unaffected**: appearance (`1`),
60-min (`2`), saves (`3`), clean sheet (`9`), DefCon (`11`) — absent from `BG` entirely, so their
only `H/A` exposure is the single factor via `BA`/`BI`'s outer multiplication, which never
compounds for them.

**Player-state dependence**: the effect **vanishes exactly at `Start%=1`** (the not-start
branch's weight is zero, verified on Raya). It scales with `(1-Start%)` for rotation players —
verified on Yoro. **Manual `Start%` overrides change only the magnitude** (via their weight on
the not-start branch), never the mechanism — verified on Saka, whose override actually produced
the *largest* magnitude sampled, from the combination of a substantial `(1-Start%)=0.4` and a
home fixture.

**Bug-or-deliberate**: this extraction documents the mechanism precisely and does not assert
author intent. Per this task's evidentiary bar, formulas *and* independent recomputation both
support describing this as a demonstrated double application — recorded in `likely_bugs`
accordingly. No comment in the workbook addresses it, and `BG` is never used independently of
`BI` anywhere that could reveal the intended behavior.

## Temporal and lookahead audit

**No lookahead risk found**, extending the proof already established in the clean-sheets/
goals-conceded and DefCon extractions to the fixture-strength chain specifically. The FDR
window block (`CONTROL!B93:B98`, feeding `TABLES`' opponent-strength figures that `PPts!R`/`S`
read) uses the identical `B3`-tracking IF-ladder offset construction already proven to keep every
window's right edge strictly behind `CONTROL!E2` (the current-GW pointer) for any value of `B3`.

Two additional, distinct findings from this extraction:

- **Fixture selection itself carries no temporal window at all.** `PPts`'s `GW`/`GW2` formulas
  and `FIXHELPER`'s DGW-detecting `COUNTIF` are pure structural/positional formulas over
  static, already-published fixture-list data — they cannot look ahead by construction, since
  there is no "current date" reference anywhere in that chain.
- **A given opponent's strength figures are identical across all 38 projected gameweeks.**
  `TABLES` computes exactly one row per team, anchored once at `CONTROL!E2` — GW1's opponent
  projection and GW38's projection against the same team use the identical frozen snapshot, with
  no in-season recency decay across the horizon itself.

Season-boundary/preseason state re-confirmed intact: `CONTROL!B3=0` places every window at the
complete previous season with zero current-season data, matching every prior extraction.

## Missing-data / promoted-team behavior

Re-confirmed and extended to the fixture-strength chain specifically: `TABLES!LF/SF xG/xGC` for
Ipswich, Coventry, and Hull are all real, nonzero figures from the same lump-sum data-population
pattern already documented in the clean-sheets/goals-conceded extraction — genuine (if coarse)
estimates, not zero-fallbacks. A direct scan of all 2000 GW1 `PPts` rows found **zero** instances
of a valid fixture paired with a zero `VS xG/90` — the `IFERROR`-to-zero fallback was never
actually triggered for any real current-roster fixture.

## Golden cases

Full inputs/formulas/cached values/recomputations are in the JSON's `golden_cases` array (4
cases covering 8 required profiles by design — several players serve double duty):

1. **Home fixture, certain starter** — David Raya (ARS, GK) vs Coventry, `H/A=1.05`,
   `Start%=1`. Isolates the pure start-branch value; the double-application mechanism is present
   in `BG` but contributes nothing to `BI` since `(1-Start%)=0`.
2. **Away fixture, rotation-prone** — Leny Yoro (MUN, DEF) vs Hull, `H/A=0.95`,
   `Start%=0.4865`. Primary double-application demonstration case.
3. **Manual Start% override, home fixture** — Bukayo Saka (ARS, MID) vs Coventry, override to
   `0.6`. Largest double-application magnitude sampled; confirms the override changes only
   magnitude, not mechanism.
4. **Promoted team, double gameweek, blank fixture** — Dara O'Shea (IPS, DEF). GW2 slot 1 (vs
   Man Utd) and slot 2 (vs Nott'm Forest, the anomalous fixture) both independently computed and
   nonzero; GW37 both slots correctly zeroed as a blank/bye.

## Unresolved ambiguities

- **`PREDICT!F` (`Start %`) scope mismatch**: uses a single-fixture `INDEX/MATCH` restricted to
  GW1 slot 1 only, unlike `D`/`E`/`J`'s full `BB2:BC2`-range `SUMIFS`. Whether intentional
  (averaging start probabilities across a multi-GW window isn't a well-defined single number the
  same way summing points is) or an oversight is not determinable from the spreadsheet alone.
- **H/A double-application intent** — mechanism proven, author intent not recoverable.
- **`BG`'s missing blank-fixture guard** — currently harmless given `BI`'s own guard, intent not
  determinable.
- **The anomalous fixture's upstream cause** — whether a genuine 2026-27 postponement, a
  data-pull timing artifact, or an uncorrected FPL-API placeholder cannot be determined from this
  workbook alone.

## Verification performed this session

```
git diff --check
ruff check .
pytest -q
git status --short
```

Every formula string recorded in the JSON was re-read from the live workbook after writing and
matched character-for-character.

No files under `src/fpl_model/`, `tests/`, `README.md`, `docs/DATA_MODEL.md`, or existing
research artifacts were modified. Files created:

- `docs/research/benchwarmers_fixture_home_away_reference.json`
- `docs/research/BENCHWARMERS_FIXTURE_HOME_AWAY_NOTES.md` (this file)

## Post-extraction validation correction

The Python golden-test translation found one arithmetic transcription error in the Saka narrative
recomputation. The recorded `BG`, `BF`, and `H/A` values imply a rate-scalable component sum of
`3.452068407311668` (`BG / BF / H/A`), not `1.0812624784446517`. The JSON method text has been
corrected accordingly. The live formula strings, cached workbook values, final `BG`/`BI`, and the
double-application conclusion are unchanged.
