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

Implemented surfaces:

- squad selection plus bank and free-transfer state;
- exhaustive legal weekly XI, captain, vice-captain, and bench order;
- a frozen three-Gameweek raw-xPts outlook;
- expected autosub value as a separate diagnostic;
- every legal, affordable same-position single transfer rescored over the same horizon;
- explicit `RESEARCH_ONLY`/`SHADOW`/`PRODUCTION` release status and pinned model-run metadata.

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
