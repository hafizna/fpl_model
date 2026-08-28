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
explanation, outlook, and squad editor. It also locks the transfer view's free-transfer and
four-point-hit banners in both modes. It runs a real `uvicorn` server in a background thread against a fixture compact
release, with `FPLClient.entry_picks` monkeypatched so no request reaches the real FPL API. Needs
the `dev` extra installed with a Chromium binary:

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
- a frozen three-Gameweek raw-xPts outlook;
- expected autosub value as a separate diagnostic;
- every legal, affordable same-position single transfer rescored over the same horizon;
- explicit `RESEARCH_ONLY`/`SHADOW`/`PRODUCTION` release status and pinned model-run metadata;
- a visible `sensitive`-recommendation warning naming the exact rotation-risk player(s) whose
  blanking would change the starting XI or captain;
- opponent/fixture (with home/away), bench depth, and confidence (projection uncertainty) on the
  three-Gameweek outlook;
- an explicit whole-scan "free transfer" vs "hit scenario" banner on the transfers view.

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

### Free transfer vs hit scenario

`recommend_web_transfers`'s `hit_cost` is uniform across one scan -- `0 if free_transfers >= 1 else
4` -- computed once from the manager's current free-transfer count, not per suggestion, since this
app does not evaluate "wait a Gameweek for a free transfer" as an alternative (see `docs/WEB_APP.md`'s
own "Current limits": no multi-Gameweek transfer planning). "Keep hit scenarios explicitly separate"
therefore means labelling the whole scan's mode clearly, not sorting individual cards -- the
frontend shows a whole-scan banner above every suggestion: green "Free transfer available" when
`suggestions[0].hit_cost == 0`, red "Hit scenario: ... costs a N-point hit" otherwise, so a manager
understands the mode before reading any individual move. This is a frontend-only change; the
backend already exposed `hit_cost` on every suggestion.

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
  three-Gameweek horizon;
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

### Loading a squad by Team ID

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

This is explicitly a no-chip comparison because chip-aware optimization is outside the MVP. The
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

## Sprint 7 score contract

The three-Gameweek screen remains an `outlook`, but it now carries a versioned benchmark-relative
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
  cumulative raw 3GW xPts, never by averaging rounded Gameweek display ratings;
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
  --model-run-id baseline_... `
  --model-run-id baseline_... `
  --model-run-id baseline_... `
  --output web\release.json
```

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
- no authentication or multi-user manager storage;
- no general multi-transfer or chip-aware optimization;
- transfer search is one move, evaluated over the frozen three-Gameweek horizon;
- percentile rating is implemented and reproducible, but remains labelled `Model Preview` until
  the underlying model release earns production approval;
- no externally configured scheduled materialisation/deployment job (the deterministic worker and
  platform-neutral container/runtime contracts exist).

The web runtime can be started directly with `scripts/run_web_app.py` (`FPL_WEB_HOST`,
`FPL_WEB_PORT`, or conventional `PORT`) or through the repository `Dockerfile`. Regardless of host,
`scripts/smoke_test_web.py` is the release-aware promotion check; it verifies that liveness,
readiness, bootstrap, catalog, horizon, and materialized rating benchmark all describe the same
immutable release.
