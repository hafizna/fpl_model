# Initial Squad Optimizer

The preseason optimizer answers a public-data question: which legal 15-player squad has the best
retained three-Gameweek mean-xPts plan? It does not need a manager screenshot or private FPL state.
It is deliberately separate from the rolling transfer planner, because preseason unlimited
transfers are an initial-selection problem rather than a one-transfer state transition.

## Inputs

The command requires three consecutive completed model runs. They must share one official FPL
snapshot, model version, and frozen `as_of`, and all must have completed by the first deadline. A
player is eligible only when the official snapshot marks them transferable and they have a
projection in all three Gameweeks. Missing projections remain excluded coverage, never zero xPts.

Create GW1 plus the frozen GW2/GW3 fixture horizon first, then run:

```powershell
python scripts/optimize_initial_squad.py `
  --model-run GW1=baseline_... `
  --model-run GW2=baseline_... `
  --model-run GW3=baseline_... `
  --output data/processed/recommendations/initial_squad_gw1_to_gw3.json
```

The default budget is £100.0m. Money is represented as integer tenths internally.

## Search and scoring

Candidate retention uses three transparent lenses within each FPL position:

- total mean xPts across the three-Gameweek horizon;
- horizon xPts per current price;
- the cheapest players needed to preserve budget-enabler coverage.

A bounded beam then constructs 2 GK / 5 DEF / 5 MID / 3 FWD squads while enforcing the £100.0m
budget and maximum three players per club. Every completed squad is rescored with the existing
exhaustive lineup engine independently in each Gameweek, including legal formation, captain,
vice-captain, and bench order. The objective is the sum of those three lineup-plus-captain xPts.

The legal and financial checks on each retained squad are exact. Candidate pruning and beam search
make the overall result approximate: it is the best retained squad, not a certificate of the global
optimum. The JSON reports the eligible pool, pruned pool, complete squads evaluated, beam width,
coverage gaps, alternatives, and the assumptions needed to reproduce the result.

## Operational boundary

Refresh the official FPL snapshot and rebuild all three compatible projection runs before using a
recommendation near the deadline. The current frozen preseason horizon changes fixture, opponent,
venue, and deadline by Gameweek, but keeps appearance, player rates, team strength, and prices
frozen to the anchor `as_of`.

The optimizer does not model future transfers, price changes, Bench Boost, or Triple Captain. Once
the manager supplies their actual squad, bank, selling prices, free transfers, and chip state, the
separate rolling planner becomes the correct personal decision boundary.

