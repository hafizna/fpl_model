# Squad tracker

The squad tracker stores an immutable, deadline-safe view of one FPL manager's team. It joins a
small manual CSV containing private manager state to a pinned official FPL snapshot containing
player identity, club, position, current price, and the target Gameweek deadline.

This split is intentional. Completed-Gameweek public picks are useful history, but the exact live
squad, purchase price, selling price, bank, and available transfers are manager-specific state.
The repository does not store FPL passwords, session cookies, or authenticated API responses.

## Manual CSV contract

The CSV contains exactly 15 rows and these columns:

```csv
fpl_id,purchase_price,selling_price,squad_position,is_captain,is_vice_captain
101,7.5,7.6,1,false,false
```

- Prices are in millions with at most one decimal place. They are converted to integer tenths on
  import so later budget comparisons do not use floating-point arithmetic.
- `squad_position` must contain every integer 1 through 15 exactly once. Positions 1 through 11
  are the starting XI; positions 12 through 15 are the bench order.
- Captain and vice-captain must be different players in the starting XI.
- Names, teams, FPL positions, and current market prices are resolved from
  `--source-ingestion-run-id`; they must not be copied into the manual file.

## Import workflow

First persist a fresh official FPL snapshot:

```powershell
.venv\Scripts\python.exe scripts/refresh_fpl_snapshot.py --season 2026-27
```

Then import the manual squad state. Before the GW1 deadline, transfers are unlimited:

```powershell
.venv\Scripts\python.exe scripts/import_squad_snapshot.py `
  --csv data/raw/squads/my_squad_2026-27_gw1.csv `
  --entry-id 123456 `
  --entry-name "My FPL Team" `
  --gameweek 1 `
  --source-ingestion-run-id fpl_YYYYMMDDTHHMMSSZ_xxxxxxxxxxxx `
  --captured-at 2026-08-20T20:00:00+07:00 `
  --source-label "manual My Team capture" `
  --bank 0.5 `
  --unlimited-transfers `
  --chip-period 1 `
  --chip-state wildcard=available `
  --chip-state free_hit=available `
  --chip-state bench_boost=available `
  --chip-state triple_captain=available
```

After the season begins, replace `--unlimited-transfers` with, for example,
`--free-transfers 2`. The official 2026/27 rules permit rolling up to five free transfers.

The importer rejects:

- a squad other than 2 GK, 5 DEF, 5 MID, and 3 FWD;
- more than three players from one Premier League club when constructing a new squad; an observed
  over-limit squad caused by a real-world club transfer is preserved with a grandfathered-limit
  flag that the next transfer recommendation must resolve;
- an invalid starting formation;
- missing or duplicate captain, vice-captain, player, or squad-position values;
- a selling price above the pinned current market price;
- players absent from the pinned official snapshot;
- an observation timestamp after the target Gameweek deadline; and
- incomplete or contradictory transfer/chip state.

Repeated import of the exact same source and metadata is idempotent. A changed file or changed
manager state creates a new snapshot rather than overwriting the old one.

## Lineup and transfer recommendations

Once the same target Gameweek has a completed `model_run`, produce an optimal legal XI, bench,
captain, and vice-captain:

```powershell
.venv\Scripts\python.exe scripts/recommend_lineup.py `
  --squad-snapshot-id squad_... `
  --model-run-id baseline_... `
  --output data/processed/recommendations/gw1_lineup.json
```

The search enumerates every legal XI. It sums double-Gameweek fixture projections before choosing
the lineup, rejects a missing squad projection rather than treating it as zero, and propagates
projection and squad-quality flags into the result.

The first transfer recommender compares no transfer with every affordable same-position
single-player swap:

```powershell
.venv\Scripts\python.exe scripts/recommend_transfers.py `
  --squad-snapshot-id squad_... `
  --model-run-id baseline_... `
  --top-n 10 `
  --output data/processed/recommendations/gw1_transfers.json
```

Each post-transfer squad is validated again and receives a fresh exhaustive XI/captain search.
Affordability uses the owned player's selling price plus bank and integer-tenths arithmetic. A
four-point cost is deducted when the manager has no free transfer; no transfer wins ties. Players
that are unavailable to transact or lack a projection are excluded and counted in the output.

This first result is not yet a season planner. It does not value future fixtures, retained free
transfers, expected price changes, chips, or risk tolerance. Those need an explicit multi-GW
objective and walk-forward evaluation before they should influence a real transfer decision.

## Storage and inspection

The relevant DuckDB tables are:

- `manager_entry`
- `squad_snapshot`
- `squad_snapshot_player`
- `squad_chip_state`

DBeaver may be used to inspect these tables and run read-only queries. Writes should go through the
validated importer so the source hash, capture time, official snapshot lineage, and rules checks
remain intact.

## Rules source

The validation constants follow the official 2026/27 FPL rules: a £100.0m initial budget, a
15-player 2/5/5/3 squad, no more than three players per club, up to five banked free transfers,
and two half-season sets of Wildcard, Free Hit, Bench Boost, and Triple Captain.

- <https://www.premierleague.com/en/news/2174419/1000>
- <https://fantasy.premierleague.com/help/>
- <https://www.premierleague.com/en/news/4679879/whats-happening-with-fpl-chips-in-202627>
