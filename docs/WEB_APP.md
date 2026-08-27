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
entering a Team ID, loading the resolved squad, and confirming the rendered pitch, outlook, and
squad editor. It runs a real `uvicorn` server in a background thread against a fixture compact
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
- a frozen three-Gameweek raw-xPts outlook;
- expected autosub value as a separate diagnostic;
- every legal, affordable same-position single transfer rescored over the same horizon;
- explicit `RESEARCH_ONLY`/`SHADOW`/`PRODUCTION` release status and pinned model-run metadata.

### Loading a squad by Team ID

`GET /api/squad/from-entry/{entry_id}?gameweek=N` fetches live from FPL's public
`entry/{id}/event/{gw}/picks/` endpoint (never `my-team/{id}/`, which is private and requires a
login session). `gameweek` defaults to the current release horizon's own start Gameweek. This
performs no server-side write -- the resolved squad (`fpl_ids`, `bank_tenths`, `selling_prices`,
captain/vice-captain) is handed straight back to the browser, which remains the only place squad
state is kept, matching this app's existing "browser local storage only" boundary.

FPL's public picks payload has no per-player purchase or selling price, so `selling_prices` is
always estimated from the CURRENT market price in the release catalog, and
`selling_price_is_estimated` is always `true` in the response -- the frontend surfaces this as a
visible caveat rather than implying an FPL-exact sell value (FPL's real selling price follows a
profit-sharing rule on price rises that cannot be reconstructed from a single picks snapshot). The
CLI/persistence equivalent, for building an immutable `squad_snapshot` database row instead of a
one-off browser fetch, is `ingest.squad_snapshot.import_squad_snapshot_from_entry`.

The three-Gameweek screen deliberately says `outlook`, not `rating`. A benchmark-relative rating
and percentile do not exist until the Sprint 7 score contract is implemented.

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
- no official percentile rating yet;
- no scheduled weekly materialisation/deployment job (the deterministic manual command exists).
