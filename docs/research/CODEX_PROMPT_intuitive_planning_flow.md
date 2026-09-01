# Codex prompt — make the Touchline planning flow update as one connected decision

Paste everything below the line into Codex (Luna). It is written to be handed to an
agent with repo access; it states the goal, the exact current-state facts it must
respect, the constraints it must not break, and a phased plan with acceptance
checks. Adjust scope by deleting phases you do not want yet.

---

## Role and repo

You are working in the `fpl_model` repository. The browser app is **Touchline — FPL
Decision Lab**: a deliberately thin, research-labelled surface over a deterministic
Python decision engine. It calculates **nothing** in JavaScript — no xPts, no
formation, no captaincy, no transfer legality. All of that comes from
`src/fpl_model/webapp/service.py`, which wraps `src/fpl_model/decision/*`.

Key files:

- `web/index.html`, `web/app.js`, `web/styles.css` — the entire frontend (vanilla
  JS, no framework, no build step). ~670 lines of JS today.
- `api/index.py` — FastAPI adapter. Endpoints: `GET /api/bootstrap`,
  `GET /api/squad/from-entry/{id}`, `POST /api/recommend/lineups`,
  `POST /api/recommend/transfers`, `GET /api/public-config`.
- `src/fpl_model/webapp/service.py` — `recommend_web_lineups`,
  `recommend_web_transfers`, `resolve_entry_picks`, `load_web_bootstrap`.
- `src/fpl_model/decision/squad.py` — `validate_squad`,
  `CHIP_NAMES = ("wildcard", "free_hit", "bench_boost", "triple_captain")`,
  `CHIP_STATUSES`, `chip_states`, `chip_period`, `unlimited_transfers`.
- `src/fpl_model/decision/transfer.py` — `apply_single_transfer`,
  `recommend_single_transfers`, `TransferOption`, `TransferRecommendation`.
- `src/fpl_model/decision/lineup.py` — `recommend_lineup`, `is_legal_starting_xi`.
- `docs/WEB_APP.md` — the app's own design record. **Update it in the same PR** for
  anything you change here; it is the source of truth for the web boundary.
- Tests: `tests/test_e2e_team_id_to_lineup.py` (real headless Chromium against a
  live uvicorn server + fixture release), `tests/test_webapp_service.py`,
  `tests/test_webapp_role_scenario_*.py`, `tests/test_web_runtime.py`,
  `tests/test_web_latency.py`.

Run the app locally:

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev,web]"
.venv\Scripts\python.exe scripts\run_web_app.py    # http://127.0.0.1:8000
.venv\Scripts\python.exe -m pytest tests/test_webapp_service.py tests/test_e2e_team_id_to_lineup.py
```

Current release: `SHADOW`, horizon GW3–GW5 (3 Gameweeks). Health gating in
`api/index.py` blocks `research` releases on Vercel but allows them locally.

## The problem (what the user actually said)

The app is "much more intuitive" than before, but **the way state flows and updates
between the Squad panel and the other menus is still disjointed**. Concretely:

1. **Transfer recommendations don't feed back into the squad.** `renderTransfers`
   in `web/app.js` only draws cards. There is no "apply this move" action anywhere —
   no button, no handler. The user sees "Salah → Saka, +2.1 net xPts" and then has
   to manually re-pick Saka in the squad editor `<select>`, which is a completely
   separate control. The transfer view and the squad are two islands.

2. **There is no chip state.** The backend hardcodes every chip to `"available"`
   (`chip_states=dict.fromkeys(CHIP_NAMES, "available")`, `service.py`) and
   `chip_period=1`. The user has no way to say "I've used my wildcard" or "I want to
   see this Gameweek with Bench Boost / Triple Captain / Free Hit on." Chip-aware
   planning is listed as a current limit in `docs/WEB_APP.md`.

3. **The hit decision is a one-line banner, not a decision aid.** `hit_cost` is a
   single whole-scan value: `0 if free_transfers >= 1 else 4`. The user wants to see
   the actual trade-off: *hold* vs *use 1 free transfer* vs *take a −4 hit for a
   second move* vs *roll the transfer to next week* — as a comparison, with the net
   xPts of each path over the visible horizon.

4. **State changes silently invalidate other views.** Editing the squad wipes the
   `current_setup` comparison and the transfer scan (with a terse string). Changing
   free-transfer count resets transfers but not lineups. Nothing ties "my squad +
   my XI + my captain + the transfer I'm considering + the chip I'm considering"
   into one coherent *plan for this Gameweek* that every menu reads from and writes
   to.

**The goal:** one connected planning object. When the user accepts a transfer, the
squad updates, the lineup re-optimises, the outlook re-scores, and the plan shows
"here is your Gameweek: this XI, this captain, this transfer, this chip, this net
xPts vs holding" — without the user re-entering anything in a second control.

## Inspiration — Solio Analytics (borrow the ideas, not the architecture)

Reference: <https://fpl.solioanalytics.com/> — a mature FPL planner. What makes its
flow feel intuitive, and which of those ideas fit Touchline's **frozen 3-Gameweek
research release** (no live re-projection, no multi-GW solver):

**Adopt now — these fit the current engine:**

- **"Bring your own beliefs" — always-available projection editing.** Solio lets you
  edit a player's projected minutes / goals / assists / clean-sheet prob and
  everything instantly re-optimises. Touchline already has exactly one primitive for
  this: `role_scenario_overrides` (`[{fpl_id, gameweek, xpts}]`, applied to a copy,
  never mutating the release). Today it's only reachable behind the sensitivity
  banner. **Make it a first-class, always-visible control**: on any player row (squad
  panel, outlook table, lineup card) an "Adjust projection" affordance that sets that
  player's xPts for a chosen horizon Gameweek and re-runs the plan. The banner's
  "Review: if X blanks" becomes one preset of this general control (set to 0).
- **Lock / ban players as optimiser constraints.** Solio: force a player in, exclude
  a player from suggestions, for a defined period. For Touchline: add
  `locked_fpl_ids` / `banned_fpl_ids` to the transfers request — a locked player is
  never the `out` leg, a banned player is never the `in` leg (and, if currently
  owned, surfaces as "you've banned this player — here's the best move out"). Cheap:
  it's just a filter in `recommend_web_transfers`'s candidate loop.
- **Risk presets that change the objective, not just the display.** Touchline's
  `RISK_PROFILES` today only filter which suggestions are *shown* (`threshold` on
  `net_xpts_gain`). Solio's risk appetite changes what's optimised. Move the risk
  profile into the request and have the backend rank suggestions by a risk-adjusted
  score — e.g. `net_xpts_gain - k * candidate_uncertainty` where `k` is
  {conservative: +, balanced: 0, aggressive: −} — using the `uncertainty` the
  projection already carries. Keep the raw `net_xpts_gain` visible alongside.
- **Named what-if branches you can compare side by side.** Instead of a single
  `roleScenarioOverrides` array plus a single staged-transfer list, allow the user to
  save the current `plan` (staged transfers + chip + projection edits) as a **named
  scenario** ("Salah blanks", "double DEF", "TC Haaland GW3"), keep 2–4 of them, and
  render their plan headers (net xPts vs holding, formation, captain) in one compare
  strip. All client-side; each scenario is just a stored `plan` snapshot re-scored
  against the same frozen release. This is Touchline's bounded answer to Solio's
  decision tree.
- **Everything re-runs on edit, no manual rebuild.** The connected-plan design below
  already delivers this; Solio is the proof that "toggle a belief → whole plan
  updates in place" is the core of the intuitive feel.

**Explicitly out of scope — state these as limits in `docs/WEB_APP.md`:**

- Solio's **branching decision tree over 12–19 Gameweeks** with a stochastic solver
  choosing transfer *and* chip timing across the whole horizon. Touchline has 3
  frozen Gameweeks and no live re-projection — it scores the plan *you* build, it
  does not search the multi-week tree.
- **Wildcard-week / chip-week targeting** ("optimise toward a GW9 wildcard").
- **GW+1 Free Hit reversion**, banked-transfer roll scored into future Gameweeks.
- **Drag-to-reorder team strength**, market-odds live refresh — Touchline's release
  is a pinned artifact, not a live model.
- Re-projecting from a minutes/goals/assists distribution. Touchline can only
  override the **already-projected xPts number**, not recompute it (see
  `RoleScenarioOverride` docstring). The projection editor is a single-number xPts
  override with that caveat shown, not Solio's component editor.

## Hard constraints — do not break these

- **No decision logic in JavaScript.** Every xPts / legality / formation / captain /
  autosub number must come from a Python endpoint. If the frontend needs a number it
  doesn't have, add it to the API response, don't compute it in `app.js`.
- **Browser-only state.** Squad, bank, free transfers, selling prices, chip state,
  and the working plan live in `localStorage` only. No server-side writes of
  manager state. This is a documented privacy boundary (`docs/WEB_APP.md`,
  `/privacy`, `/terms`).
- **The frozen release is immutable.** Reviewed scenarios and chip toggles produce a
  *new working copy* of the projections for one request (see
  `apply_role_scenario_overrides` — copy, never mutate). The published
  `web/release.json` and the squad-rating benchmark never move because of a UI
  toggle.
- **The rating benchmark is fixed.** `_squad_rating_payload` always benchmarks
  against `base_projections`. A chip or scenario may change the *submitted squad's*
  score but must not move the comparison population or the scale. Keep
  `reviewed_scenario` / an equivalent `chip_scenario` flag on the response so the UI
  can label a what-if.
- **Decision receipts stay intact.** Every lineup/transfer response carries
  `decision_receipt_v1` via `attach_decision_receipt`. Any new request field must be
  included in the hashed request payload so the receipt stays reproducible.
- **Keep the vanilla-JS, no-build frontend.** Do not introduce React, a bundler, or
  a package.json for `web/`. Small ES modules are fine if you must split `app.js`.
- **Accessibility parity.** New controls need labels, `aria-live` on regions that
  update, and keyboard operability, matching the existing pattern.
- **`docs/WEB_APP.md` is updated in the same change** and its "Current limits"
  section reconciled.
- **Tests updated, not deleted.** `tests/test_e2e_team_id_to_lineup.py` locks the
  free-transfer and −4-hit banners today; if you restructure that view, update the
  assertions to the new contract rather than removing them.

## Design of the connected plan

Introduce a single client-side object, persisted to `localStorage` as
`touchline-plan`, that is the one source of truth every view reads:

```
plan = {
  squad: number[15],              // current fpl_ids (already `state.selected`)
  bank_tenths, free_transfers,
  selling_prices: {fpl_id: tenths},
  current_setup: {...} | null,    // submitted XI/C/VC/bench from Team ID load
  chip: null | "bench_boost" | "triple_captain" | "free_hit" | "wildcard",
  chip_status: {                  // what the user has already used this season
    wildcard: "available" | "used",
    free_hit: "available" | "used",
    bench_boost: "available" | "used",
    triple_captain: "available" | "used",
  },
  pending_transfers: [            // moves staged but toggleable, applied to a copy
    { out_fpl_id, in_fpl_id }
  ],
  projection_edits: [             // Phase 4 — "bring your own beliefs"
    { fpl_id, gameweek, xpts }    // same shape as role_scenario_overrides
  ],
  locked_fpl_ids: number[],       // Phase 4 — never transfer these out
  banned_fpl_ids: number[],       // Phase 4 — never suggest these in
  horizon_length, risk_profile,
}

// Phase 4 — saved named branches, each a full plan snapshot re-scored
// against the same frozen release. Not part of `plan`; its own key
// `touchline-scenarios`.
scenarios = [ { id, name, plan: <plan snapshot>, saved_at } ]   // keep 2–4
```

- The **Squad panel** edits `squad`, `bank_tenths`, `free_transfers`,
  `selling_prices`, and now `chip_status` (four "available / used" toggles) and the
  active `chip` for this Gameweek.
- The **Transfers view** proposes moves; **"Apply move"** on a card pushes
  `{out, in}` into `plan.pending_transfers` and immediately re-runs lineups + outlook
  from the resulting squad. Pending transfers render as a removable chip strip at the
  top of the Squad panel AND the Transfers view ("Staged: Salah → Saka ✕"). A
  **"Commit to squad"** action folds `pending_transfers` into `squad`, decrements
  `free_transfers` (charging the hit if the count goes negative), and clears the
  staging list. Nothing is committed silently.
- The **Lineup and Outlook views** always render from
  `squad + pending_transfers + chip`, so accepting a transfer or toggling Bench
  Boost updates them in place. Show a compact "Plan for GW{n}" header on the Lineup
  view: formation, captain, chip (if any), staged transfer count, and **net xPts vs
  holding** (baseline = current committed squad, no chip, no staged transfer).

## Backend changes (`api/index.py` + `service.py`)

1. **Accept chip + transfer plan on both recommend endpoints.** Extend
   `SquadRequest`:

   ```python
   chip: Literal["wildcard", "free_hit", "bench_boost", "triple_captain"] | None = None
   chip_status: dict[str, Literal["available", "used"]] = Field(default_factory=dict)
   pending_transfers: list[PendingTransfer] = Field(default_factory=list)  # {out_fpl_id, in_fpl_id}
   # Phase 4:
   locked_fpl_ids: list[int] = Field(default_factory=list)   # transfers endpoint only
   banned_fpl_ids: list[int] = Field(default_factory=list)   # transfers endpoint only
   risk_profile: Literal["conservative", "balanced", "aggressive"] = "balanced"
   # projection_edits reuses the existing `role_scenario_overrides` field —
   # do not add a second one.
   ```

   Every new field must be included in `_request_payload` so `decision_receipt_v1`
   still hashes the full request.

   Thread `chip` / `chip_status` into `_validated_web_squad` instead of the hardcoded
   `dict.fromkeys(CHIP_NAMES, "available")` and `chip_period=1`. When `chip` is set,
   pass `chip_states={chip: "active", ...}` and `unlimited_transfers=True` for
   `wildcard`/`free_hit` (matches `validate_squad`'s own rule at
   `squad.py:148`). Validate that an `"active"` chip is not also `"used"`.

2. **Chip scoring in `_lineup_payload` / `recommend_web_lineups`:**
   - `bench_boost`: total xPts includes all 4 bench players' projected points (the
     autosub EV term is replaced by the summed bench xPts). Return a
     `chip_effect: {chip, delta_xpts}` block.
   - `triple_captain`: captain contributes `2x` extra instead of `1x` (so `3x`
     total). Reuse `recommend_lineup`'s captain search; apply the extra multiplier
     in the payload total and in `chip_effect.delta_xpts`.
   - `free_hit`: score the *proposed* squad for the first horizon Gameweek only,
     and explicitly note in `method_note` that the squad reverts next Gameweek (the
     app does not plan GW+1 reversion — state that as a limit).
   - `wildcard`: no per-Gameweek scoring change; it only sets
     `unlimited_transfers=True` so the transfer scan is unconstrained by FT count
     and charges no hit. Make the transfers endpoint honour that.

3. **Richer hit / path comparison on `recommend_web_transfers`.** Replace the single
   whole-scan `hit_cost` with an explicit `paths` array the frontend can render
   side by side, each scored over the visible horizon:

   ```
   paths: [
     { id: "hold",        label: "Hold",                    transfers: [],      hit: 0,  net_xpts: <baseline> },
     { id: "one_ft",       label: "Use 1 free transfer",     transfers: [best1], hit: 0,  net_xpts: ... },
     { id: "hit_minus4",   label: "2 moves, -4 hit",         transfers: [b1,b2], hit: 4,  net_xpts: ... },   // only if a positive-net 2-move exists
     { id: "roll",         label: "Roll the transfer",       transfers: [],      hit: 0,  net_xpts: <hold>, note: "banked FT next GW; this app does not score GW+1" },
   ]
   recommended_path_id: "one_ft"
   ```

   Keep it bounded: at most the single best 1-move and the single best affordable
   2-move (do not blow up the brute-force scan — `recommend_web_transfers` already
   iterates every legal single move; the 2-move step should reuse the top-N single
   moves as the first leg, not do a full N² rescan). If `chip == "wildcard"` or
   `"free_hit"`, the paths collapse to "unlimited moves, no hit" and you surface the
   best *set* of moves the existing scan finds greedily (document the greedy caveat).

4. **`load_web_bootstrap` unchanged** except: if it's cheap, add each player's
   `status` reason so the UI can explain why an injured player is excluded from
   transfer targets (optional, nice-to-have).

5. **Keep every response's `decision_receipt`** — add `chip`, `chip_status`,
   `pending_transfers`, `locked_fpl_ids`, `banned_fpl_ids`, `risk_profile` to
   `_request_payload` so the hash covers them.

6. **Phase 4 — lock / ban / risk ranking in `recommend_web_transfers`:**
   - candidate loop skips `out_id in locked_fpl_ids` and `in_id in banned_fpl_ids`;
   - if a banned id is currently owned, still evaluate moves *out* of it and mark
     those suggestions `forced_out_reason: "banned"`;
   - final `suggestions.sort` key becomes risk-adjusted:
     `-(net_xpts_gain - K[risk_profile] * candidate_horizon_uncertainty)` with
     `candidate_horizon_uncertainty` the RSS of the candidate's per-Gameweek
     `uncertainty` (already on each lineup payload; `None` → treat as 0 and note it);
   - keep raw `net_xpts_gain` and a new `risk_adjusted_gain` both on every
     suggestion so the UI shows both. `K` example: conservative 0.5, balanced 0.0,
     aggressive −0.5 — put the constants in one named dict with a comment.

## Frontend changes (`web/app.js`, `web/index.html`, `web/styles.css`)

1. **Plan object + migration.** Replace the scattered `state.selected`,
   `state.sellingPrices`, `state.currentSetup`, `state.horizonLength`,
   `state.riskProfile` reads/writes with a single `state.plan` persisted as
   `touchline-plan`. Migrate existing `localStorage` keys on first load (read old
   keys, build `plan`, write it, keep old keys for one release for safety).

2. **Squad panel additions:**
   - A "Chips" block: four toggles (Wildcard / Free Hit / Bench Boost / Triple
     Captain), each `available` ⇄ `used`, plus a single "Play this Gameweek"
     selector (radio: none / one of the *available* chips). Wildcard and Free Hit
     visually distinct (they change the whole scan). Persist to
     `plan.chip_status` / `plan.chip`.
   - A "Staged transfers" chip strip (shared component with the Transfers view),
     each removable, with a "Commit to squad" button that folds them in and adjusts
     `free_transfers` / charges the hit, then re-runs everything.

3. **Transfers view:**
   - Each suggestion card gets an **"Apply move"** button → pushes to
     `plan.pending_transfers`, re-runs `runLineups()`, switches focus to the staged
     strip (do not auto-navigate away — show a toast/inline confirmation).
   - Render the new `paths` array as a compact comparison (Hold / 1 FT / −4 hit /
     Roll), net xPts each, the `recommended_path_id` highlighted. Clicking a path's
     "Stage this" applies its `transfers` to `plan.pending_transfers`.
   - Remove the old single free/hit banner; the E2E test assertions move to the new
     `paths` comparison (assert the "Hold" and a "-4 hit" row render with numeric
     net xPts).

4. **Lineup view — "Plan for GW{n}" header:** formation · captain (· chip badge if
   active) · "{k} staged transfer(s)" · **net xPts vs holding** (green/red). This is
   the single glue element that makes the flow feel connected — every accepted
   transfer or chip toggle changes this number in place.

5. **Consistent invalidation, not silent wipes.** When squad / FT / chip / staged
   transfers change:
   - re-run `runLineups()` automatically (it already reads the plan),
   - mark the transfer scan stale with a *specific* message ("Squad changed — Saka
     is now in. Re-scan to compare from the new squad.") and a one-click re-scan,
   - never clear `current_setup` just because a *staged* (uncommitted) transfer
     exists — only clear it on a committed squad edit, and show "comparison is vs
     your submitted GW{n} XI; staged moves shown separately".

6. **Settings view:** move the chip-status editor's explanation here too, and keep
   risk profile / horizon where they are. No functional change beyond wiring to
   `plan`.

## Phasing (ship in this order; each phase is independently mergeable)

- **Phase 1 — Plan object + apply-transfer-to-squad.** Introduce `state.plan` +
  migration. Add `pending_transfers` to the request. "Apply move" / staged strip /
  "Commit to squad". Lineup + Outlook re-render from `squad + pending_transfers`.
  "Plan for GW{n}" header with net-xPts-vs-holding. No chips yet, no paths yet.
  *This alone fixes pain points 1 and 4.*
- **Phase 2 — Hit / path comparison.** Replace the single hit banner with the
  `paths` array (hold / 1 FT / −4 / roll), backend + frontend. *Fixes pain point 3.*
- **Phase 3 — Chips.** `chip_status` + active `chip`, backend scoring for
  bench_boost / triple_captain / free_hit / wildcard, Squad-panel chip block, chip
  badge in the plan header, chip effect on Outlook. Reconcile `docs/WEB_APP.md`
  "Current limits". *Fixes pain point 2.*
- **Phase 4 — Solio-inspired modifiability.** In order of value:
  1. **Always-on projection editing** (`projection_edits`, reusing the
     `role_scenario_overrides` request field and the copy-not-mutate machinery). An
     "Adjust projection" control on every player row; the sensitivity banner's
     "if X blanks" becomes a preset. An "edits active" chip strip with one-click
     reset, mirroring the staged-transfer strip.
  2. **Lock / ban** (`locked_fpl_ids` / `banned_fpl_ids` on the transfers request;
     filter in the candidate loop). Small lock/ban toggles on squad rows and
     transfer-target rows.
  3. **Risk profile drives ranking** — move `risk_profile` into the transfers
     request; rank by `net_xpts_gain - k * candidate_uncertainty`; show raw net gain
     too. Retire the display-only `threshold` filter or keep it as a secondary
     "hide marginal moves" switch.
  4. **Named scenarios** (`touchline-scenarios`, 2–4 saved `plan` snapshots) with a
     compare strip showing each scenario's plan header. Pure frontend; each snapshot
     is re-scored through the same endpoints against the same frozen release.
  *This is the "modifiable model" the user asked for, bounded to what the frozen
  release supports.*

## Acceptance checks

Phase 1:
- Load a Team ID → Transfers → "Apply move" on the top suggestion → the Squad panel
  shows the incoming player staged, the Lineup pitch re-renders with them, and the
  "Plan for GW{n}" net-xPts-vs-holding is positive and matches
  `suggestion.net_xpts_gain` for a 1-move within one free transfer.
- "Commit to squad" folds the move in, decrements free transfers 1→0, and a second
  staged move then shows "−4 hit" in the header.
- Reloading the page restores the same plan (squad + staged + chip) from
  `localStorage`.
- `tests/test_e2e_team_id_to_lineup.py` extended: apply a transfer, assert the pitch
  DOM changes and the plan header shows a net-xPts delta.

Phase 2:
- Transfers view shows a Hold / 1 FT / −4 / Roll comparison with numeric net xPts
  for each and one highlighted recommendation.
- With `free_transfers = 0`, the "1 FT" path is absent or disabled and the −4 path
  is the cheapest non-hold option.
- Backend `recommend_web_transfers` returns `paths` and `recommended_path_id`;
  `tests/test_webapp_service.py` covers a `free_transfers=0` and a
  `free_transfers=2` case.

Phase 3:
- Toggling Bench Boost raises the Outlook total by roughly the summed bench xPts and
  the plan header shows a "Bench Boost" badge; toggling it off restores the number.
- Triple Captain raises the GW total by the captain's projected points again (3x not
  2x) and `chip_effect.delta_xpts` matches.
- Marking Wildcard "used" removes it from the "play this Gameweek" options; setting
  Wildcard active makes the transfer scan charge no hit for multiple moves.
- The squad-rating percentile does NOT change when a chip is toggled (benchmark is
  still `base_projections`); the response carries a `chip_scenario: true` flag and
  the UI labels it a what-if.
- `docs/WEB_APP.md` updated: chip section added, "no chip-aware optimization" removed
  from Current limits, free/hit banner section replaced with the paths contract.

Phase 4:
- "Adjust projection" on a player row, set GW3 xPts, and the Lineup/Outlook/plan
  header all re-score; the response carries `is_reviewed_scenario: true`; the squad
  benchmark percentile is unchanged. An "edits active" strip lists the edit and
  resets it in one click.
- Banning an owned player surfaces a best move-out suggestion; locking a player
  removes it from every `out` leg in the scan. `tests/test_webapp_service.py` covers
  a locked and a banned id.
- Switching risk profile from balanced to conservative reorders the suggestion list
  (a lower-uncertainty move rises); raw `net_xpts_gain` still displayed. Backend test
  asserts the ranking key changes with `risk_profile`.
- Save two scenarios ("hold", "TC Haaland"), see both plan headers side by side with
  their net-xPts-vs-holding; reload restores them.

## Non-goals (state these as limits in `docs/WEB_APP.md`)

- No multi-Gameweek transfer planning (paths are scored over the current visible
  horizon only; "roll" does not score GW+1).
- No Free Hit GW+1 reversion modelling.
- No optimal chip-timing recommendation across the season — the app scores the chip
  *you choose* for *this* Gameweek, it does not tell you which week to play it.
- Wildcard/Free Hit multi-move selection stays greedy (reuses the single-move scan),
  not a global squad re-optimisation.
- No Solio-style branching decision tree over 12–19 Gameweeks and no stochastic
  solver — Touchline scores the plan the user builds against 3 frozen Gameweeks.
- The projection editor overrides an already-projected xPts number only; it does not
  re-project from a minutes/goals/assists distribution (no live re-projection
  pipeline exists in the web boundary).
- Named scenarios are client-side plan snapshots re-scored against the same frozen
  release; they are not a saved server-side planning history.
