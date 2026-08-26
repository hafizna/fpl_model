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

## Locked and excluded players (scenario comparisons)

`--lock FPL_ID` (repeatable) forces a player into every returned squad; `--exclude FPL_ID`
(repeatable) forces one out. This is the mechanism for the structural counterfactual comparisons
design principle 6 requires -- for example, run the command once with `--lock <Haaland's fpl_id>`
and once with `--exclude <Haaland's fpl_id>` to compare a Haaland vs no-Haaland squad, or lock one
premium goalkeeper against excluding it (forcing a cheaper set-and-forget-plus-backup structure) to
test the goalkeeper sanity check design principle 6 also requires:

```powershell
python scripts/optimize_initial_squad.py `
  --model-run GW1=baseline_... --model-run GW2=baseline_... --model-run GW3=baseline_... `
  --lock 123456 `
  --output data/processed/recommendations/initial_squad_locked_haaland.json
```

A locked player must still be legal on its own: it must have a complete, transferable projection in
all three Gameweeks, its position must not already be full among the locks, it must not push any
club over the three-player limit, and the locked players' combined price must not alone exceed the
budget. Any violation raises before the search runs, rather than silently dropping the lock or
producing an infeasible result. A player cannot be both locked and excluded. The command's JSON
output echoes the resolved `constraints` so a scenario run is reproducible from its own output.

## Dominance audit

The beam search is approximate: candidate pruning can miss a legal, cheaper-or-equal, higher-xPts
squad entirely -- this is exactly what happened in the documented 22 August 2026 diagnostic (a
188.68 xPts squad with two GBP 5.0m goalkeepers and a benched GBP 8.0m Watkins, dominated by a
manually locked Raya/Dubravka structure scoring 189.09 xPts at the same budget). `--audit-dominance`
re-runs the search up to three more times using the same locked/excluded mechanism above, targeting
three named structural counterfactuals design principle 6 requires:

- `cheap_goalkeeper_pair` -- excludes the recommended squad's own most expensive goalkeeper;
- `cheap_bench_reinvestment` -- excludes every non-goalkeeper bench player above the cheap-enabler
  price threshold who was benched in every retained Gameweek;
- `premium_starter_reinvestment` -- "premium sanity": excludes the single most expensive
  premium-priced player (at least `PREMIUM_PRICE_MARGIN_TENTHS` above the cheap-enabler price for
  their own position) who is never captained across the retained horizon -- a player commanding a
  premium price must earn it through marginal horizon value, not merely a squad slot.

Each counterfactual's own recommended squad is compared against the original using the standard
Pareto-dominance rule: a counterfactual dominates when its cumulative three-Gameweek xPts is at
least as high AND its cost is at most as high, with at least one strict inequality. The output
JSON's `dominance_audit.is_dominated` flag, `dominating_counterfactuals`, and each counterfactual's
own comparison are all reported; nothing about the original recommendation changes automatically --
a dominated result should be treated as `RESEARCH_ONLY` and reviewed, not silently replaced. This
audits three named counterfactuals only, not an exhaustive proof against every possible structure,
and each counterfactual is itself the same approximate beam search with one player excluded.

## Operational boundary

Refresh the official FPL snapshot and rebuild all three compatible projection runs before using a
recommendation near the deadline. The current frozen preseason horizon changes fixture, opponent,
venue, and deadline by Gameweek, but keeps appearance, player rates, team strength, and prices
frozen to the anchor `as_of`.

The optimizer does not model future transfers, price changes, Bench Boost, or Triple Captain. Once
the manager supplies their actual squad, bank, selling prices, free transfers, and chip state, the
separate rolling planner becomes the correct personal decision boundary.

