# Targeted Player-Rate Evidence

This boundary stores reviewed evidence for current FPL players who lack usable previous-Premier-
League rate history. It is intentionally small and targeted: the project does not need a database
of every player in every source league.

Evidence is not a production prior. Importing a row does not write `player_rate_history`, does not
close a `baseline_projection_gap`, and does not change xPts or the squad optimizer. A separate,
backtested translation/shrinkage policy is required before any external statistic can enter the
baseline.

## Targeted workflow

Export the currently ordered gap list as a prefilled evidence template. To focus first on players
outside the three promoted clubs:

```powershell
python scripts/export_player_rate_evidence_template.py `
  --model-run baseline_... `
  --exclude-team COV `
  --exclude-team HUL `
  --exclude-team IPS `
  --output data/raw/manual/player_rate_evidence_non_promoted.csv
```

Delete rows that are not worth researching and complete the evidence columns for the retained
players. Context columns such as research rank, expected minutes, team, price, and ownership are
ignored by the importer; they exist only to prioritize manual work.

Validate and persist the reviewed rows against the exact official FPL snapshot used by the model:

```powershell
python scripts/import_player_rate_evidence.py `
  --csv data/raw/manual/player_rate_evidence_non_promoted.csv `
  --source-ingestion-run fpl_... `
  --target-gameweek 1 `
  --source-label "targeted manual external-rate research"
```

Player ID, code, name, and position must match that pinned snapshot. Every row requires an
observation timestamp, source reference, and rationale. Blank provider fields remain `NULL`; they
must never be changed to zero merely to satisfy a schema.

## Comparability classes

- `senior_comparable`: meaningful senior first-team evidence from a competition considered a
  plausible translation candidate. It still requires a validated league-translation policy.
- `senior_non_comparable`: senior evidence whose competition, role, or level is not safely
  comparable. Raw rates are retained as context only.
- `academy_youth`: academy or youth competition evidence. Even a player bought from a major club's
  academy belongs here when their statistical sample is not senior first-team football. Youth
  xG/xA must not be treated as Premier League ability.
- `role_only`: sourced evidence about senior-squad role or readiness with no statistical rate.
  Every rate field must remain blank. A separate reviewed appearance-scenario override is the
  correct path when that evidence supports expected playing time.

All rows receive `RESEARCH_EVIDENCE_NOT_PRODUCTION_RATE`. Additional flags identify academy,
non-comparable, role-only, and partial-stat cases.

## What happens next

After enough targeted evidence exists, candidate translation policies can be assessed separately:

1. derive a position baseline from observed Premier League history;
2. define source-competition translation and sample-size shrinkage without using the target
   player's future Premier League outcomes;
3. backtest the policy on historical transfers and promoted players;
4. compare coverage and out-of-sample error against the current explicit-gap baseline;
5. only then create a new versioned production prior policy.

Academy/youth and role-only rows may never justify a direct raw-rate translation. They can still
support uncertainty, manual review priority, and appearance-role evidence.

