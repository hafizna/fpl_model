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
- explicit `RESEARCH` release status and pinned model-run metadata.

The three-Gameweek screen deliberately says `outlook`, not `rating`. A benchmark-relative rating
and percentile do not exist until the Sprint 7 score contract is implemented.

## Vercel boundary

Vercel can host the frontend and its Python runtime can host FastAPI. The local DuckDB file is
generated and gitignored, however, and must not be treated as durable serverless state. Before a
real deployment, export an approved compact projection release to durable object storage or a
serverless analytical database, and store private manager state in an external transactional
database such as Neon Postgres.

The intended deployed split is:

```text
Vercel static frontend / lightweight API
                    |
                    +-- approved read-only projection release
                    +-- external manager-state database
                    +-- Python decision service for expensive searches
```

The current API adapter is `api/index.py`. `FPL_DATABASE_PATH` may point it at a different local or
mounted DuckDB file. Do not deploy the ignored development database or use a function's local
filesystem for persistent squad writes.

## Current limits

- research/shadow projection release only;
- no authentication or multi-user manager storage;
- no general multi-transfer or chip-aware optimization;
- transfer search is one move, evaluated over the frozen three-Gameweek horizon;
- no official percentile rating yet;
- no automatic weekly materialisation job.
