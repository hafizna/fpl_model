# Browser recommender MVP

The browser app is a thin, research-labelled surface over the existing Python decision engine. It
does not calculate xPts, formations, captaincy, or transfer legality in JavaScript.

## Local run

Install the optional web dependencies and start the application from the repository root:

```powershell
.venv\Scripts\python.exe -m pip install -e ".[web]"
.venv\Scripts\python.exe scripts\run_web_app.py
```

Open <http://127.0.0.1:8000/>. The initial browser state is seeded with the current 15-player test
squad, then stored only in that browser's local storage. The app never asks for or stores an FPL
password or session cookie.

## End-to-end tests

`tests/test_e2e_team_id_to_lineup.py` drives the actual served page in a real headless Chromium
browser (not just the FastAPI `TestClient`), covering "Team ID to weekly decision without a CLI":
entering a Team ID, loading the resolved squad, and confirming the rendered pitch, marginal-change
explanation, outlook, and squad editor. It also covers staging one transfer and checking that the
connected plan header and pitch update, plus the scored Hold / free-transfer / -4 / Roll paths,
toggling Bench Boost in place (plan header badge, outlook total, marking the chip used and reloading)
and a Free Hit move that is free but cannot be committed. It runs a real `uvicorn` server in a background thread against a fixture compact release, with
`FPLClient.entry_picks` monkeypatched so no request reaches the real FPL API. Needs the `dev` extra
installed with a Chromium binary:

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev,web]"
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe -m pytest tests/test_e2e_team_id_to_lineup.py
```

Implemented surfaces:

- loading a public squad by FPL Team ID (`GET /api/squad/from-entry/{id}`) -- no password or
  session cookie, ever; manual squad selection plus bank and free-transfer state remains available
  as an override;
- exhaustive legal weekly XI, captain, vice-captain, and bench order;
- marginal no-chip xPts and explained XI/captain/vice/bench-order changes against the manager's
  current submitted setup loaded by Team ID;
- a frozen one-to-five-Gameweek raw-xPts outlook (published horizon length; the visible slice is a
  browser-only Setting);
- expected autosub value as a separate diagnostic;
- every legal, affordable same-position single transfer rescored over the same horizon;
- explicit `RESEARCH_ONLY`/`SHADOW`/`PRODUCTION` release status and pinned model-run metadata;
- a visible `sensitive`-recommendation warning naming the exact rotation-risk player(s) whose
  blanking would change the starting XI or captain;
- opponent/fixture (with home/away), bench depth, and confidence (projection uncertainty) on the
  outlook;
- an explicit server-scored Hold / transfer / hit / Roll comparison on the transfers view;
- a server-scored Wildcard-versus-hold/free-transfer comparison over the published horizon
  (`POST /api/recommend/wildcard`), using the manager's actual selling prices and preserved free
  transfers, with an explicit post-Wildcard roll period;
- browser-held chip state (which chips are spent this half-season) and one chip played as a what-if
  for the first horizon Gameweek -- Bench Boost, Triple Captain, Free Hit, or Wildcard -- scored by
  the Python service across the plan header, lineup, outlook, and transfer scan.

### Connected plan (Phase 1)

The browser persists one working plan under `localStorage["touchline-plan"]`:
`squad`, `bank_tenths`, `free_transfers`, `selling_prices`, `current_setup`,
`pending_transfers`, `horizon_length`, `risk_profile`, `chip`, and `chip_status` (the last two
added in Phase 3; older stored plans default to no chip and every chip available). On first load it migrates the earlier
`touchline-squad`, `touchline-selling-prices`, `touchline-selling-estimated`,
`touchline-current-setup`, `touchline-horizon`, and `touchline-risk-profile` keys into that object;
the legacy keys remain for one release as a safety fallback. Manager state is never written to the
server.

Transfer recommendations expose an `Apply move` action. This adds an `{out_fpl_id, in_fpl_id}`
record to `pending_transfers`, then sends the whole staged list to both recommendation endpoints.
The Python service validates and applies those moves to an in-memory working squad copy using the
same squad rules as the decision layer; the frozen release and rating benchmark are not mutated.
Like FPL's own confirm step, the staged set is validated as one batch: every move must sell an
owned player for an available same-position player, and budget plus the three-per-club limit are
checked on the final squad rather than after each intermediate move. Lineup responses include a `plan_summary` with the effective squad, bank, free transfers,
formation, captain, staged count, and server-computed `net_xpts_vs_holding`, so JavaScript never
calculates transfer legality or xPts. Decision receipts hash the complete request, including staged
moves, and remain reproducible.

The Squad panel and Transfers view share a removable staged-transfer strip. `Commit to squad`
folds the server-validated effective state into the browser plan, decrements each free transfer (a
`4.0` point hit is included in `pending_hit_cost` once the count is exhausted), clears pending
moves, and clears the submitted-XI comparison because the committed squad has changed. A staged
move is not silently committed; removing it or committing it always re-runs lineup/outlook scoring.
Projection editing, locks/bans, risk-adjusted backend ranking, and named scenarios remain Phase 4
of the planning-flow brief (`docs/research/CODEX_PROMPT_intuitive_planning_flow.md`).

### Fixtures, bench depth, and confidence

`load_horizon_catalog`'s SQL query now also selects `player_fixture_projection.opponent_team_id`/
`is_home` (joined a second time against `team_snapshot` for the opponent's short name), attaching a
`fixtures: [{opponent_team_id, opponent, is_home}]` list to each player's own
`gameweeks[gw]` entry -- a list rather than a single fixture, since a double Gameweek genuinely has
more than one. This is threaded through `_player_payload` (now also carrying `uncertainty` from
`PlayerGameweekProjection`, which already existed but was not previously surfaced) via a new
`_fixtures_for_gameweek` helper, so every player object in a lineup/outlook response --
captain, vice-captain, starters, bench -- carries its own Gameweek's fixture(s) and uncertainty. The
outlook view renders the captain's fixture on each Gameweek card, a "Bench depth" line (summed bench
xPts for that Gameweek), and a Fixtures/Confidence column in the player table (confidence shows `—`
when uncertainty is `null`, which is the current production release's own shadow-stage state --
calibrated uncertainty is not yet applied to production projections).

Note: `combine_appearance_probability` (`decision/lineup_store.py`) is shared across four call
sites and unpacks its input as a fixed 5-element tuple -- fixture/opponent data is deliberately kept
in a separate `opponent_rows` structure in `load_horizon_catalog` rather than widening that shared
tuple, which would have broken every other caller.

### Transfer-path comparison (Phase 2)

`recommend_web_transfers` now returns a `paths` array that is fully scored by the Python decision
service. Every path has `net_xpts` (after that path's hit), `delta_xpts_vs_hold`, a server-provided
transfer list, and a `recommended_path_id`; the browser only renders those values and may stage the
returned moves. The compact comparison contains Hold and Roll every time, plus the best legal
single free transfer when one is available. With no free transfer, its best single move is labelled
`Take a −4 hit`; with one free transfer, that −4 path is a two-move candidate. With two or more
free transfers it can instead show a two-free-transfer path.

The two-move candidate is intentionally bounded: it pairs only the strongest eight single-move
candidates, revalidates the combined squad and affordability, and re-scores the resulting XI and
captaincy over the frozen horizon. It is not a global two-transfer optimizer. A two-move path is
shown only if it improves on Hold, and a path requiring more than one hit is omitted. Roll is shown
as a transparent reminder that the next Gameweek is not scored: `Banked FT next Gameweek; this app
does not score GW+1.`

### Chips (Phase 3)

`POST /api/recommend/lineups` and `POST /api/recommend/transfers` accept two optional fields:

- `chip_status`: `{wildcard|free_hit|bench_boost|triple_captain: "available"|"used"}` for the
  current half-season (FPL's second chip set opens at GW20; the service derives `chip_period` from
  the horizon's first Gameweek instead of hard-coding it);
- `chip`: at most one chip to play in the **first** horizon Gameweek. Playing a chip marked `used`
  is rejected with `422`.

Both fields are part of the hashed request, so `decision_receipt_v1` stays reproducible. The
browser keeps them in `touchline-plan` only, with a Chips block in the Squad panel (a radio group
for the chip to play, plus an `Available`/`Used` toggle per chip; marking the active chip used
clears it) and an explanation card in Settings.

Scoring (all in `webapp/service.py`; JavaScript only renders it):

- **Bench Boost** adds the four bench players' projected points to the first Gameweek total;
- **Triple Captain** adds the captain's projected points once more (3x in total). Neither points
  chip changes the optimal XI or captain, so the exhaustive no-chip lineup is kept and only the
  total moves. Combined uncertainty is widened to match (bench variances added; captain standard
  deviation weighted 3x instead of 2x);
- **Wildcard** makes staged transfers free, keeps the saved free-transfer count, and keeps the
  rebuilt squad; weekly scoring is otherwise unchanged. `Commit to squad` then marks the Wildcard
  used and clears the active chip;
- **Free Hit** makes staged transfers free for the first Gameweek only. Later horizon Gameweeks are
  scored from the committed squad Free Hit reverts to, held with no further transfers. Because the
  squad reverts, `plan_summary.commit_allowed` is `false` and the browser disables `Commit to squad`.

The first Gameweek's lineup payload carries `chip_effect: {chip, label, delta_xpts, note}`
(`delta_xpts` is `0` for Wildcard/Free Hit, whose value is in the transfers). `plan_summary` adds
`chip`, `chip_label`, `chip_effect_xpts`, `commit_allowed`, and `squad_reverts_after_gameweek`;
`net_xpts_vs_holding` still compares against the committed squad with **no chip and no staged
transfer**, so switching Bench Boost on raises it by the summed bench xPts. Responses carry `chip`,
`chip_status`, and `chip_scenario: true` whenever a chip is played, and the outlook labels the
rating as chip-free.

The squad rating never includes the chip: benchmark squads are never scored with one, so
`squad_rating` is computed from the no-chip lineups (for Free Hit, from the committed squad it
reverts to). Toggling a chip therefore never moves the percentile or the benchmark.

On the transfers endpoint, every suggestion and path is scored with the chosen chip played. Under
Bench Boost or Triple Captain the Phase 2 Hold / FT / −4 / Roll comparison is unchanged apart from
that. Under Wildcard or Free Hit every move is free and saved transfers are preserved, so the
comparison collapses to **Hold**, **Best single move** (no hit), and a rebuilt **Wildcard squad** /
**Free Hit squad**. The rebuild reuses the Wildcard squad search (`decision/wildcard.py`, bounded
beam, owned players priced at their sale value) over the full horizon for Wildcard or the first
Gameweek alone for Free Hit, is paired into same-position moves the browser can stage, and is
re-scored through the same staged-transfer path so staging it reproduces the path's number. If the
bounded search finds no legal rebuild the path is omitted rather than failing the scan. Free Hit
suggestions score only their own Gameweek, because later Gameweeks revert.

### Wildcard comparison

`POST /api/recommend/wildcard` extends the squad request with `horizon_length` (2--5),
`roll_after_wildcard` (0--4), `terminal_ft_value` (0--10, an assumed points value per free transfer
still banked after the final horizon deadline), and optional `locked_fpl_ids` / `excluded_fpl_ids`
constraints for the Wildcard squad search. It requires no staged transfers -- the comparison starts
from the committed squad -- and returns three fully server-scored paths:

- **Hold** -- roll every Gameweek in the horizon;
- **Use free transfers** -- the best bounded roll / one-transfer / atomic two-transfer path;
- **Wildcard + roll** -- a Wildcard squad chosen for the horizon, then a forced roll for
  `roll_after_wildcard` Gameweeks, then free transfer paths for the rest.

Each path carries per-Gameweek XI, captain, bank, hits, and free-transfer transitions, plus
`net_xpts` (cumulative lineup/captain xPts minus hits) and an `objective` that adds the assumed
terminal free-transfer value. Wildcard affordability uses bank plus **actual sale proceeds**;
retaining an owned player costs their sale value as opportunity cost and preserves their real
market/purchase/selling values afterwards (no reset to £100m, no sell-and-rebuy spread on retained
players). Preserved free transfers are carried through the Wildcard deadline without granting an
extra one, matching the [official saved-transfer rules](https://www.premierleague.com/news/4661029).

The comparison always starts from the no-chip committed squad: a request with an active `chip` or
with `chip_status.wildcard == "used"` is rejected with `422`, and the browser disables the button
while the Wildcard is marked used.

The response is `decision_status: RESEARCH_ONLY`, pinned to the release, and carries a decision
receipt (`decision_type: wildcard_comparison`). It never changes the actual FPL team, Wildcard
availability is otherwise *assumed* for the what-if, and the future option value of keeping the chip for a
later week is **not** priced -- a small Wildcard gain is not by itself a reason to play the chip.
The Wildcard squad search and the two-transfer bundle search are both bounded candidate/beam
searches, not certified global optima; results carry an `APPROXIMATE_BUNDLE_SHORTLIST` flag where a
proxy shortlist was used. The full contract, weekly workflow, and worked free-transfer table live
in `docs/WILDCARD_FIVE_GAMEWEEK_PLANNER.md`.

### Sensitive-decision state

`role_state` (from `validation/role_state.py`) is baked into `web/release.json` per player per
Gameweek by `webapp/release_export.py`, the same way `transparency` already is -- no DuckDB
connection is available in the compact-release deployment mode, so this has to be pre-computed at
export time rather than queried per request. `webapp/service.py`'s `_lineup_payload` reads it back
from the already-loaded catalog (no second database/release read) and runs
`decision/role_scenario_sensitivity.py`'s `evaluate_role_scenario_sensitivity` against the base
recommendation, attaching the result as `role_scenario_sensitivity` on every lineup response. The
frontend shows an amber banner naming the player(s) driving a `sensitive` label. This is
deliberately baseline-only on `POST /api/recommend/transfers` -- computed for the current squad's
own lineup, not for every candidate transfer -- since that endpoint already brute-forces hundreds
of candidates and re-running `recommend_lineup` per rotation-risk player per candidate would
multiply an already expensive scan. `export_web_release.py` must be re-run whenever the release is
rebuilt for this to stay current; a release built before this feature existed simply has no
`role_state` field and the frontend banner stays hidden rather than erroring.

### Freshness and coverage

`build_web_release` stores the full `orchestrate_release_validation` freshness report (per-Gameweek
snapshot age, fixture finality, `is_final`) as `release.freshness`, and a player-level coverage
count as `release.coverage`:

- `total_registered_players`: every player in the official snapshot this release's source ingestion
  run captured;
- `fully_covered_players`: how many of them have a projection in EVERY Gameweek of the release's
  published horizon (one to five Gameweeks);
- `excluded_missing_projection`: registered players absent from the release's catalog entirely (no
  projection for any horizon Gameweek);
- `excluded_partial_horizon_coverage`: players present in the catalog but missing one specific
  Gameweek's projection (a postponed or blank fixture) -- these are excluded from the release rather
  than shipped with a hole, since `load_release_catalog`'s read side requires every catalog player
  to carry every horizon Gameweek.

`webapp/service.py` threads both through `load_web_bootstrap`/`recommend_web_lineups`/
`recommend_web_transfers` as `coverage`/`freshness` (`None` in database-connected mode, which has no
precomputed freshness/coverage gate). The sidebar release card renders a compact summary, e.g.
"594/612 players covered" and "0/3 GW final".

### Reviewed role-scenario overrides

`POST /api/recommend/lineups`/`POST /api/recommend/transfers` accept an optional
`role_scenario_overrides` list: `[{"fpl_id": ..., "gameweek": ..., "xpts": ...}]`. This is
deliberately narrower than a full appearance-scenario override
(`context/minutes.py`'s reviewed start/cameo distribution): the web app has no live re-projection
pipeline to call from a request (projections are baked into the release at export time, and
re-running the model is DB-only and expensive), so a reviewed scenario here can only replace one
already-projected xPts number for one player in one Gameweek, not recompute it from a
start/substitute/sixty-minute distribution.

`webapp/service.py`'s `apply_role_scenario_overrides` returns a NEW projections mapping -- it never
mutates the loaded release/catalog, so the base release stays exactly as published; only that one
request's working copy differs. Every other field of the projection (uncertainty, appearance
probability, quality flags) is left as the release's own original values, since this overrides a
reviewed point estimate, not a new projection. A response built from at least one override sets
`is_reviewed_scenario: true` and, on the lineups endpoint, an adjusted `method_note`.

The frontend's sensitivity banner (see above) doubles as the entry point: each rotation-risk player
named in a `sensitive` label gets a "Review: if NAME blanks" button that sets that player's xPts to
0 for the current Gameweek and recomputes the weekly/outlook/transfer views from it. A green
"Reviewed scenario active" banner replaces the warning while a scenario is active, with a "Back to
base release" control that clears it. Stale transfer-scan results are invalidated (not just
discarded in memory) whenever the scenario or free-transfer count changes, since `POST
/api/recommend/transfers` is only re-run when the user explicitly re-scans.

### Loading a squad by Team ID or public URL

`GET /api/squad/from-entry/{entry_id}?gameweek=N` fetches live from FPL's public
`entry/{id}/event/{gw}/picks/` endpoint (never `my-team/{id}/`, which is private and requires a
login session). `gameweek` defaults to the current release horizon's own start Gameweek. This
performs no server-side write -- the resolved squad (`fpl_ids`, `bank_tenths`, `selling_prices`,
submitted XI, ordered bench, and captain/vice-captain) is handed straight back to the browser,
which remains the only place squad state is kept, matching this app's existing "browser local
storage only" boundary.

### Marginal changes against the current setup

"Marginal" means the model recommendation versus the manager's current submitted FPL picks, not
versus recommendation history. `POST /api/recommend/lineups` accepts that optional current-setup
snapshot and the Python decision service scores both setups from the same first-Gameweek frozen
projections. The response includes current/recommended raw totals, marginal xPts, separate
starting-XI and captain gains, players started/benched, captain and vice changes, and whether the
bench order changed. The browser renders the reasons; it does not recompute points.

This stays a no-chip comparison even while a chip is played: it explains XI and captaincy choices,
and the chip's own effect is shown separately in the plan header. The
snapshot is stored in browser local storage alongside the squad and is cleared as soon as a player
is edited or the projection horizon changes, so the app cannot silently compare against stale
picks. A manually assembled squad has no submitted XI/C/VC baseline, so the Weekly menu asks the
user to load a Team ID instead of inventing one. Historical recommendation persistence is a
separate future feature and is not needed for this contract.

FPL's public picks payload has no per-player purchase or selling price, so `selling_prices` is
always estimated from the CURRENT market price in the release catalog, and
`selling_price_is_estimated` is always `true` in the response -- the frontend surfaces this as a
visible caveat rather than implying an FPL-exact sell value (FPL's real selling price follows a
profit-sharing rule on price rises that cannot be reconstructed from a single picks snapshot). The
CLI/persistence equivalent, for building an immutable `squad_snapshot` database row instead of a
one-off browser fetch, is `ingest.squad_snapshot.import_squad_snapshot_from_entry`.

The browser accepts either a numeric Team ID or a public `fantasy.premierleague.com/entry/<id>`
URL. Supplying `include_profile=true` also returns the public entry name when available; profile
metadata is optional and a profile lookup failure does not block picks. FPL's public API does not
provide a reliable name-search endpoint, so the UI explains that constraint instead of pretending
name search is supported.

Navigation is staged as Setup, Lineup, Outlook, Transfers, and Settings. Settings currently
controls the visible slice of the published horizon (1–5 options, with unavailable lengths
disabled) and a browser-only decision stance for transfer shortlist display. The underlying
release and projection model remain unchanged until a longer horizon or calibrated risk model is
materialized.

## Sprint 7 score contract

The outlook screen carries a versioned benchmark-relative
`Model Score` (`Model Preview` while the release is not production-approved). The scale is not a
min/max of open browser scenarios:

- `optimized_xi_captain_percentile_v1` compares the submitted squad's optimized-XI-plus-captain
  xPts with a deterministic rank-weighted sample of legal squads generated from the same frozen
  base release and the exact same current-price budget cap; the sampler reinvests spare budget
  into a £5m band below that cap and retains 128 distinct squads (minimum valid population 100);
- the benchmark identity includes release/horizon identity, budget, candidate population, search
  settings, and raw benchmark scores; reviewed role scenarios rescore the submitted squad but do
  not move this benchmark;
- each Gameweek percentile is calculated separately; the overall percentile is calculated from
  cumulative raw horizon xPts, never by averaging rounded Gameweek display ratings;
- model strength, data-quality flags, projection uncertainty, legal-squad health, and release
  approval are separate response fields and separate UI labels;
- fewer than 100 legal benchmark squads causes the percentile to be withheld while raw xPts stays
  available.

`build_web_release` now materializes six reusable budget-anchor populations (£90m through £115m,
128 squads each) into `release.rating_benchmark`. At request time the service selects a frozen
128-squad population whose members are all legal under the manager's exact current-price budget
cap. This removes lineup-population optimization from the request path. A legacy research/shadow
release may temporarily fall back to an in-process runtime cache; a production release may not:
`/api/ready` fails and the score is withheld unless the materialized artifact is `ready`.

The full rating payload (benchmark identity and inputs, raw scores, percentile, and explanation)
is returned by the lineup API and retained only as `touchline-last-squad-rating` in browser local
storage. This follows the app's existing privacy boundary: no manager-specific rating is written
to the server. The same baseline rating is returned by Transfers, and Weekly uses the matching
first-Gameweek percentile. `release_drift_v1` records percentile and benchmark-identity changes
when a manager squad is supplied for provisional-to-final comparisons.

Validate the request-path contract before promotion:

```powershell
.venv\Scripts\python.exe scripts\check_web_latency.py `
  --release web\release.json `
  --fpl-id <repeat exactly 15 times> `
  --output outputs\web_latency_report.json
```

`web_latency_contract_v1` requires a stable materialized benchmark, stable raw xPts, an available
rating, cold decision latency no more than 3 seconds, and the repeated/cached decision no more than
1 second. It stores no Team ID and only records squad size, timings, release/benchmark identities,
and pass/fail checks.

## Vercel boundary

Vercel can host this FastAPI entrypoint. The app now prefers the packaged, immutable
`web/release.json` compact release, so recommendation requests do not need the generated and
gitignored DuckDB file. Generate or replace it only from a release that passes manifest and
freshness validation:

```powershell
.venv\Scripts\python.exe scripts\export_web_release.py `
  --model-run-id baseline_...  # one --model-run-id per Gameweek, ascending, 1-5 total `
  --output web\release.json
```

The current packaged release is the `coherent_benchwarmers_inseason_baseline_v2` five-Gameweek
horizon (GW4--GW8). v2 updates attacking, DefCon, and saves inputs from final official prior-Gameweek
evidence with small-sample shrinkage; earlier `v1` releases are not rewritten. The Wildcard
comparison endpoint requires a horizon of at least the requested `horizon_length`, so a shorter
packaged release will reject a longer Wildcard request with `422`.

Use `--require-production` only after calibration and uncertainty artifacts are genuinely
approved. Without it, a passing shadow release remains usable but is visibly labelled `SHADOW`.
`FPL_WEB_RELEASE_PATH` can select another packaged/mounted artifact; `FPL_DATABASE_PATH` remains
the local-development fallback.

The intended deployed split is:

```text
Vercel static frontend / lightweight API
                    |
                    +-- immutable read-only compact projection release
                    +-- external manager-state database
                    +-- Python decision service for expensive searches
```

The current API adapter is `api/index.py`. Browser squad state remains in local storage; there are
no server-side private writes in this MVP. A later authenticated multi-user version still needs an
external transactional manager-state store.

## Current limits

- research/shadow projection release only;
- controlled-alpha tester-code gate only; no account authentication, entitlement, or multi-user
  manager storage;
- no general multi-transfer search; without Wildcard/Free Hit the transfers view compares one move
  and a bounded shortlist-derived two-move alternative only, while multiple transfers can still be staged
  manually in the browser plan and committed only after review;
- chips are scored only for the chip *you choose* in the first horizon Gameweek; no endpoint
  recommends *which week* to play a chip, and neither the chip view nor the Wildcard comparison
  prices the future option value of keeping a chip;
- Free Hit is scored as one Gameweek on the rebuilt squad followed by the committed squad held
  unchanged; transfers after the reversion are not planned;
- Wildcard/Free Hit rebuilds use the bounded Wildcard squad search, not a certified global squad
  optimum; chip status is entered by hand in the browser (it is not read from FPL's chip history);
- transfer and Wildcard paths are evaluated over the frozen published horizon (one to five
  Gameweeks); Roll does not score the Gameweek after the horizon ends;
- percentile rating is implemented and reproducible, but remains labelled `Model Preview` until
  the underlying model release earns production approval;
- no externally configured scheduled materialisation/deployment job (the deterministic worker and
  platform-neutral container/runtime contracts exist).

The web runtime can be started directly with `scripts/run_web_app.py` (`FPL_WEB_HOST`,
`FPL_WEB_PORT`, or conventional `PORT`) or through the repository `Dockerfile`. Regardless of host,
`scripts/smoke_test_web.py` is the release-aware promotion check; it verifies that liveness,
readiness, bootstrap, catalog, horizon, and materialized rating benchmark all describe the same
immutable release.

For a controlled alpha, the operator can require individually labelled access codes. Only SHA-256
digests are configured on the server; the plaintext code is sent in `X-FPL-Alpha-Token` and retained
in the browser's `sessionStorage` for that session only. Decision/catalog routes are protected while
`/api/live` and `/api/ready` remain available to monitors. Per-code limits are process-local (60
protected requests and two transfer scans per minute by default), so this boundary is intentionally
limited to a small single-instance alpha and is not presented as public authentication or global
abuse protection.

`/api/public-config`, `/privacy`, and `/terms` form the public closed-alpha operations boundary. A
required alpha cannot serve protected decisions unless operator name, support email, host/region,
log retention, and the explicit legal-review flag are configured. These pages describe the current
no-account/no-payment/no-analytics data flow and link the official Indonesian PDP law; they still
require review against the actual operator and chosen host before testers are invited.

Each lineup/outlook and transfer response also carries `decision_receipt_v1`. The stable decision ID
hashes the exact request/response and immutable release while the downloadable receipt exposes only
hashes, release/horizon, notice versions, and non-persistence status—not the squad, Team ID, selling
prices, or access code. This gives support a reproducible reference without creating a server-side
manager-history database.
