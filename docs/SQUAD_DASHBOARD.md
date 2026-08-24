# Local squad scenario dashboard

The local squad dashboard is the first browser-facing presentation layer for the decision engine.
It is inspired by the workflow of established FPL tool suites—move from the current squad to
player projections, fixture context, and transfer comparisons—but it keeps this project's own
explainability and data-quality boundaries.

The dashboard is intentionally a generated, standalone HTML file rather than a notebook. A
notebook remains useful for research, but the operational question is repetitive: compare a small
number of named squads against the same frozen three-Gameweek horizon. The HTML opens directly in
a browser, needs no server or external JavaScript, and makes scenario differences visible without
exposing database details.

## Build the current comparison

```bash
python scripts/build_squad_dashboard.py \
  --scenario "Scenario A=data/raw/manual/current_squad_reconciled_2026_27.csv" \
  --scenario "Candidate v2=data/raw/manual/current_squad_candidate_v2_2026_27.csv" \
  --model-run GW1=baseline_5c9232cf56b2fd05 \
  --model-run GW2=baseline_52e73d70884212e8 \
  --model-run GW3=baseline_e1655adb4d0c1e2c \
  --source-ingestion-run fpl_20260820T122504Z_dd92b92253a7 \
  --output outputs/squad_scenarios_current.html
```

Scenario CSVs require exactly 15 unique `fpl_id` values plus `purchase_price` and
`selling_price`. `squad_position`, `is_captain`, and `is_vice_captain` may either all be filled or
all be blank. Private manager state stays in the CSV; names, clubs, positions, current prices, and
availability are joined from the explicitly pinned official FPL snapshot.

## What it shows

- tabs for comparing alternate 15-player scenarios;
- an FPL-style pitch that groups the selected XI by formation, with a separate ordered bench;
- Gameweek tabs that keep one relevant xPts value in focus instead of showing three competing
  numbers on every player card;
- an XI xPts total for the selected Gameweek, including the captain multiplier, only when all 11
  starters are covered;
- current budget, official-snapshot legality, and three-GW projection coverage;
- transfers in/out relative to the first scenario;
- compact captain, vice-captain, and projection-risk markers on the pitch;
- per-player GW1–GW3 xPts and explicit gap flags in an on-demand player detail dialog;
- research-evidence status for gaps, without treating that evidence as a production projection;
- the covered-player xPts delta between scenarios;
- an optimized XI/captain recommendation only when all 15 players are projected.

The main view is intentionally optimized for football scanning: formation first, active Gameweek
second, and data diagnostics on demand. The complete 15-player projection table remains available
under **Projection audit**. Team shirts are lightweight local CSS markers rather than copied FPL or
third-party artwork.

The dashboard deliberately withholds a supposedly optimal lineup when projection coverage is
incomplete. Covered-player xPts is a diagnostic sum across every covered owned player, not the
starting-XI objective. It must not be used to treat an unprojected player as zero-value.

## Current boundary and next increment

The current 2026/27 comparison uses an official player snapshot newer than the frozen Aug-17 model
runs. It is therefore a scenario inspection surface, not a deadline-final transfer instruction.
The next useful increment is to ingest targeted evidence for the three missing players, refresh
the frozen horizon, and then show the rolling planner's legal transfer path, hits, bank state,
lineups, uncertainty, and explanations in this same presentation layer.

References for workflow inspiration:

- [Fantasy Football Hub My Team](https://www.fantasyfootballhub.co.uk/my-team/pick)
- [Fantasy Football Hub predictions](https://www.fantasyfootballhub.co.uk/predictions)
- [Fantasy Football Hub fixture ticker](https://www.fantasyfootballhub.co.uk/fixture-ticker)
- [Fantasy Football Hub player comparison](https://www.fantasyfootballhub.co.uk/fpl-player-comparison-tool)
