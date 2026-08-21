# Canonical player identity bridge

The bridge makes player identity explicit across the official FPL API and Vaastav. It never joins
players by name.

## Identity contract

- `canonical_player_id` is the official FPL `code`, which Vaastav retains as `code` in
  `players_raw.csv`.
- `official_fpl.provider_player_id` is the season-local official `id` (`fpl_id` in this project).
- `vaastav.provider_player_id` is the season-local Vaastav `id` from the pinned
  `players_raw.csv`.
- `shared_player_code` is the only automatic cross-provider match method.
- Names are retained only to raise `NAME_MISMATCH`; they are not fallback keys.
- Current-only and historical-only players remain visible with `MISSING_VAASTAV_ID` or
  `MISSING_OFFICIAL_FPL_ID` rather than being silently dropped.

Every import is content-addressed by the pinned official FPL ingestion run, seasons, Vaastav
revision, source-file hash, and bridge policy version. The source CSV itself remains under
`data/raw/` and is not committed.

## Import

Use the `players_raw.csv` archived from a pinned Vaastav revision, not `player_idlist.csv`:
`player_idlist.csv` contains season-local IDs and names but does not contain the stable `code`.

```bash
python scripts/import_player_identity_bridge.py \
  --vaastav-players-csv data/raw/vaastav/2025-26/players_raw.csv \
  --source-ingestion-run-id fpl_snapshot_... \
  --target-season 2026-27 \
  --vaastav-season 2025-26 \
  --source-revision <pinned-git-sha>
```

The command writes `player_identity_bridge_run` and `player_identity_bridge`. A
`completed_with_gaps` result is expected when the current squad contains promoted-team players or
new signings absent from the previous Premier League season. Those gaps must receive an explicit
prior elsewhere; the bridge does not invent one.
