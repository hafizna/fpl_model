# Platform-neutral deadline refresh worker

`scripts/run_deadline_refresh.py` is the operational wrapper around the existing deterministic
in-season materialization and compact web export. It is deliberately independent of Vercel or any
other host: a scheduler only needs to run one Python command, preserve its working directory and
DuckDB file, inspect its exit code, and retain the JSON artifacts.

## What the worker guarantees

1. An exclusive lock prevents two refreshes from writing the same local artifacts concurrently.
2. The current DuckDB file is copied to a timestamped backup directory before mutation when it
   exists. Retention/deletion is intentionally left to the operator; the worker never deletes an
   old backup.
3. `materialize_inseason_release` fetches official data, builds the three-Gameweek horizon, attaches
   calibration/uncertainty, and runs the release gates.
4. A failed materialization or validation never calls the compact exporter.
5. The compact release is fully built beside the published file and moved into place atomically.
   Until that final replace, the previous `web/release.json` remains byte-for-byte unchanged.
6. A machine-readable status file is written atomically on success or pipeline failure.
7. An optional generic webhook receives a privacy-safe summary. Its URL comes only from an
   environment variable and is never written to status/log output.

Immutable rows written to DuckDB before a later stage fails remain for diagnosis; no existing run
is overwritten. The published web artifact is the fail-closed boundary.

## Command

```powershell
$env:FPL_REFRESH_WEBHOOK_URL = "https://your-alert-adapter.example/fpl-refresh"

.venv\Scripts\python.exe scripts\run_deadline_refresh.py `
  --gameweek 3 `
  --current-season 2026-27 `
  --previous-season 2025-26 `
  --team-strength-csv data\raw\workbooks\benchwarmers_team_strength_2026_27.csv `
  --team-strength-source-label "MODEL.xlsx reviewed team-strength export" `
  --vaastav-players-csv data\raw\vaastav\2025-26\<pinned-revision>\players_raw.csv `
  --vaastav-source-revision <pinned-revision> `
  --calibration-artifact-id <approved-or-shadow-artifact-id> `
  --uncertainty-artifact-id <approved-or-shadow-artifact-id>
```

Defaults:

- database: `data/processed/fpl.duckdb`;
- published artifact: `web/release.json`;
- status: `outputs/deadline_refresh_status.json`;
- full materialization report: `outputs/inseason_release_materialization.json`;
- lock: `outputs/deadline_refresh.lock`;
- backups: `outputs/backups/`.

Use `--require-production` only when the release must fail unless calibration and uncertainty are
approved for production. A closed alpha may omit it and publish an honestly labelled `shadow`
release. `--allow-analytically-complete` remains the explicit provisional escape hatch described in
`docs/INSEASON_REFRESH.md`; a scheduler must never add it automatically after a finality failure.

## Exit codes and status

| Exit | Meaning | Published release |
|---|---|---|
| `0` | pipeline succeeded; webhook delivered or not configured | new release |
| `1` | worker setup, active lock, or status-write exception; inspect stderr | unchanged unless the exception followed the atomic publish |
| `2` | materialization, validation, export, backup, or publish failed | previous release retained unless failure happened after the atomic replace |
| `3` | release succeeded, but configured webhook delivery failed | new release |

An existing lock raises immediately and does not overwrite the active worker's status file. Inspect
the PID/host/start time in the lock before removing a stale lock manually.

Status schema `deadline_refresh_status_v1` includes:

- run and target Gameweek;
- start/finish time and duration;
- success/failure stage plus a concise error type/message;
- previous and newly published release IDs;
- release health and model-run lineage on success;
- backup/report/output paths;
- alert status (`delivered`, `failed`, or `not_configured`).

The webhook payload adds `event=fpl_deadline_refresh` and includes the same summary, never the
webhook URL, database contents, manager squads, or Team IDs. Use a small adapter/workflow if Slack,
Discord, email, or another destination requires its own message schema.

## Scheduling without choosing a host

The worker can run from Windows Task Scheduler, a persistent CI runner, a VPS service timer, or a
managed job runner. The scheduler must provide:

- a persistent working directory and DuckDB/artifact storage;
- network access to official FPL endpoints;
- the reviewed input files and pinned artifact IDs;
- two useful deadline runs (for example T-6h and T-75m) plus an external missing-run alert;
- a timeout longer than the full materialization pipeline;
- secure environment-variable injection for the webhook URL;
- retention for status, materialization reports, database backups, and logs.

Do not use an ephemeral runner unless it explicitly restores and republishes the database plus all
required immutable inputs. Do not use the web request process as the worker. The concrete scheduler
adapter and its credentials remain deployment choices; the worker contract does not.
