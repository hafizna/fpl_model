# Closed-alpha deployment runbook

This runbook covers the current stateless browser application and decision API. It does not turn
the projection refresh into a web request. The deployable artifact is the immutable
`web/release.json`; DuckDB remains a local/worker-side build input.

## Architecture boundary

```text
external refresh worker (stateful, scheduled separately)
    -> validated immutable web/release.json
    -> Git commit / preview deployment
    -> Vercel FastAPI function + browser UI
    -> browser-local manager squad state
```

Vercel detects `api/index.py` as one FastAPI function. The function may read the packaged compact
release and calculate decisions, but must not refresh FPL data, write DuckDB, or persist manager
state. A refresh can exceed request limits and needs durable artifacts plus precise deadline
scheduling; it belongs in the separately monitored, platform-neutral
`scripts/run_deadline_refresh.py` worker documented in `docs/DEADLINE_REFRESH_WORKER.md`.

## Runtime contract

- Python is pinned by `.python-version` to 3.13.
- `vercel.json` caps the FastAPI function at 30 seconds and excludes research/build-only files from
  the Python bundle. The real release's single-transfer scan was approximately 13.8 seconds on the
  development machine on 28 August 2026; 30 seconds leaves headroom without allowing runaway
  invocations.
- `/api/live` checks only that the process can respond.
- `/api/ready` and the backward-compatible `/api/health` parse and validate the compact release,
  then expose its ID, health label, planning timestamp, horizon, and catalog size. They return 503
  when the artifact is invalid or unavailable.
- Every response carries `X-Request-ID`. Application logs use the FastAPI route template rather
  than the raw URL, preventing a public FPL Team ID from being written to logs.
- `FPL_TRANSFER_SCAN_ENABLED=false` is the emergency cost kill switch. Weekly and outlook decisions
  continue to work while the expensive transfer endpoint returns 503.
- On Vercel, API docs default off and allowed release health defaults to `shadow,production` even if
  their environment variables were omitted. Local development defaults remain permissive.

## Environment configuration

Start from `.env.example` locally. Do not upload `.env` or treat the browser bundle as a secret
store.

| Variable | Preview | Production/alpha | Secret |
|---|---|---|---|
| `FPL_WEB_RELEASE_PATH` | omit to use packaged release | omit to use packaged release | no |
| `FPL_DATABASE_PATH` | omit | omit | no |
| `FPL_WEB_HOST` | `127.0.0.1` | `0.0.0.0` for a container | no |
| `FPL_WEB_PORT` / `PORT` | `8000` | platform-assigned port or `8000` | no |
| `FPL_EXPOSE_API_DOCS` | `true` | `false` | no |
| `FPL_LOG_LEVEL` | `INFO` | `INFO` | no |
| `FPL_ALLOWED_RELEASE_HEALTH` | `research,shadow,production` | `shadow,production` for alpha; `production` when approved | no |
| `FPL_TRANSFER_SCAN_ENABLED` | `true` | `true` for controlled alpha | no |
| `FPL_ALLOWED_ORIGINS` | local/preview origin if cross-origin | exact frontend origin if API is split | no |

The current stateless alpha has no application secret because it has no account system, payment
provider, cron endpoint, or server-side manager database. Do not invent a shared frontend API key:
anything shipped to browser JavaScript is public. Authentication/entitlement and a global platform
rate limit are required before opening transfer scans beyond a controlled alpha. A future cron
endpoint must use a server-side `CRON_SECRET` of at least 16 random characters.

## Platform-neutral container path

`Dockerfile` is the portable web-runtime contract. It installs only the application and web
dependencies, includes the immutable `web/release.json`, excludes raw/processed databases and
development artifacts through `.dockerignore`, runs as the unprivileged `fpl` user, and checks
`/api/ready` rather than treating process liveness as decision readiness.

```powershell
docker build -t fpl-model-web:local .
docker run --rm --read-only --tmpfs /tmp -p 8000:8000 `
  -e FPL_ALLOWED_RELEASE_HEALTH=shadow,production `
  fpl-model-web:local
```

The image is stateless and supports a read-only root filesystem. A new projection release means
building a new immutable image; do not mount a mutable database into the public web process. Hosts
that inject `PORT` can run `python scripts/run_web_app.py` directly or override the image's default
port 8000 without rebuilding it.

After any local, preview, or production start, verify release identity and the complete runtime
boundary with the same command:

```powershell
.venv\Scripts\python.exe scripts\smoke_test_web.py `
  --base-url http://127.0.0.1:8000 `
  --expected-release-id <EXPECTED_RELEASE_ID> `
  --allowed-health shadow,production
```

`web_runtime_smoke_v1` fails unless liveness, readiness, and bootstrap agree on one release ID,
health state, three-Gameweek horizon, non-empty catalog, and ready rating benchmark. This command is
the provider-independent promotion check; provider-specific logs, rollback, and cost alarms remain
additional external setup.

## Release and preview procedure

1. Build the three-Gameweek release only through the validated exporter:

   ```powershell
   .venv\Scripts\python.exe scripts\export_web_release.py `
     --model-run-id <GW_N_RUN> `
     --model-run-id <GW_N_PLUS_1_RUN> `
     --model-run-id <GW_N_PLUS_2_RUN> `
     --output web\release.json
   ```

2. Run repository verification:

   ```powershell
   .venv\Scripts\python.exe -m ruff check .
   .venv\Scripts\python.exe -m pytest -q
   node --check web\app.js
   git diff --check
   ```

3. Install Vercel CLI 48.1.8 or newer, link the project, and create a preview deployment:

   ```powershell
   npm install --global vercel
   vercel link
   vercel
   ```

4. Verify the immutable preview before promotion:

   ```powershell
   vercel inspect <preview-url>
   vercel curl /api/live --deployment <preview-url>
   vercel curl /api/ready --deployment <preview-url>
   vercel httpstat / --deployment <preview-url>
   vercel logs --deployment <preview-url> --level error --limit 50
   ```

   The ready response must show the expected `release_id`, three consecutive Gameweeks, a non-empty
   catalog, and the intended `shadow` or `production` health. For a closed alpha, `shadow` must
   remain visibly labelled; it must not be relabelled as production.

   Run `scripts/smoke_test_web.py` against the preview URL as the portable equivalent of these
   release-consistency assertions.

5. Smoke-test Team ID loading, Weekly, Outlook, one free-transfer scan, and one hit scan in the
   preview UI. Then promote:

   ```powershell
   vercel promote <preview-url>
   vercel promote status
   vercel logs --environment production --level error --since 5m
   vercel httpstat /
   ```

## Monitoring and alerts

Use a monitor outside Vercel so a Vercel-wide incident cannot report itself as healthy.

- Request `/api/live` and `/api/ready` every five minutes. Alert after two consecutive failures.
- Assert the expected release ID and horizon; HTTP 200 alone is insufficient.
- Assert `rating_benchmark_status=ready` and record `rating_benchmark_id`; production readiness
  fails closed when the benchmark is missing or stale.
- Alert on any readiness 503, more than five HTTP 5xx responses in five minutes, or transfer-scan
  duration over 25 seconds.
- Before every deadline, alert separately if the scheduled refresh did not publish the expected
  release ID. That schedule belongs to the next P2 work item, not a Vercel Hobby cron.
- Keep structured runtime logs for incident diagnosis, but do not add raw request URLs, request
  bodies, squad lists, or Team IDs.

## Cost limits

Vercel Hobby does not create a bill, but exhausting included compute makes the service unavailable.
The production-release benchmark of roughly 13.8 seconds per transfer scan means repeated scans,
not page views, dominate compute. Ten to twenty controlled alpha users are reasonable; 200 users
are not yet an evidence-backed launch target.

- Keep `maxDuration` at 30 seconds.
- Run `scripts/check_web_latency.py` before promotion. The lineup/rating path must use
  `release_artifact`, stay below 3 seconds cold and 1 second repeated, and return stable raw xPts
  plus benchmark identity.
- Use the transfer kill switch during abuse or unexpected latency.
- Do not advertise a public transfer endpoint until authentication and platform-global rate limits
  exist.
- Review Vercel Usage after each alpha deadline. On Pro, configure Spend Management notifications
  at 50%, 75%, and 100%, with the production-pause action at the agreed hard amount. Spend
  Management is team-wide, so verify other projects before enabling automatic pause.

## Backup and rollback

The web tier is stateless. Its recoverable source of truth is the Git commit plus the content-hashed
`web/release.json` inside an immutable deployment. Record the current commit, deployment URL, and
release ID after each promotion. Browser-local manager state is convenience state and has no server
backup guarantee.

For a bad application or release deployment:

```powershell
vercel rollback
vercel curl /api/ready
vercel logs --environment production --level error --since 5m
```

Confirm that the restored release ID and horizon are the intended known-good values. Vercel instant
rollback does not rebuild with new environment variables; diagnose configuration incidents
separately. After a rollback, production-domain auto-assignment is disabled until another deployment
is promoted. A future external account/manager database must add provider-native point-in-time
recovery and a restore drill before paid beta; it is not covered by this stateless backup contract.

## Incident quick actions

1. Transfer scan slow or abused: set `FPL_TRANSFER_SCAN_ENABLED=false`, redeploy, verify Weekly and
   Outlook, then inspect logs and usage.
2. Invalid/stale release: do not bypass readiness. Roll back to the last known-good release and stop
   promotion of new artifacts until refresh validation passes.
3. Application 5xx after deploy: instant rollback, verify `/api/ready`, retain the failed deployment
   URL/logs for diagnosis.
4. Vercel incident: publish status through a separate channel; do not run the stateful refresh
   pipeline inside an ad-hoc function invocation.

Current platform references: [FastAPI on Vercel](https://vercel.com/docs/frameworks/backend/fastapi),
[Python runtime](https://vercel.com/docs/functions/runtimes/python),
[Cron limits](https://vercel.com/docs/cron-jobs/usage-and-pricing),
[Spend Management](https://vercel.com/docs/spend-management), and
[Instant Rollback](https://vercel.com/docs/instant-rollback).
