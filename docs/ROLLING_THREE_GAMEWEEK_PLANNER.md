# Rolling three-Gameweek planner

## Decision policy

The intended operating cadence is to create one plan for three consecutive Gameweeks and normally
review it after the third. The plan contains an action, lineup, bench, captain, and vice-captain for
each Gameweek. It may be regenerated earlier only when new evidence creates an emergency trigger:

- confirmed injury or material availability downgrade;
- suspension or red card;
- real-world transfer or registration change; or
- material starting-role change supported by current evidence.

This is a decision-layer policy. It does not amend the frozen 2026/27 appearance-calibration
confirmatory protocol, which remains an upstream model evaluation.

## Projection contract

The command requires exactly three consecutive model runs. They must:

- be completed no later than the GW N deadline and target GW N, N+1, and N+2;
- use the official FPL ingestion pinned by the squad snapshot;
- share one model version;
- share one frozen `as_of` timestamp no later than the GW N deadline; and
- contain a projection for every player retained or acquired along a plan path.

The canonical preseason baseline materializer still produces GW1 only. A separate frozen-horizon
materializer now produces GW+1/GW+2 by rerunning all components against those GWs' own fixtures,
opponents, and venues. It freezes appearance, player-rate, team-strength, and minutes inputs to the
anchor and flags that assumption on every future row. It fails on missing fixture/deadline metadata
rather than copying GW1 xPts forward.
The horizon identity is versioned independently as `frozen_preseason_fixture_horizon_v1` while the
underlying model runs retain the anchor baseline model version for compatibility checks.

```powershell
.venv\Scripts\python.exe scripts/project_frozen_horizon.py `
  --anchor-model-run-id baseline_...
```

## Objective and state transitions

The current objective maximizes cumulative mean xPts across three GWs after immediate transfer-hit
costs. Each GW independently optimizes the legal XI and captaincy. State includes the full squad,
bank, player selling values, and free transfers.

- Rolling adds one free transfer, capped at five.
- One transfer consumes one free transfer when available.
- A transfer beyond the available count costs four points.
- Affordability uses bank plus the outgoing player's selling value.
- A purchased player enters at the frozen current price.
- Every post-transfer squad must satisfy position and club constraints.

Active chips are rejected. Prices are frozen through the horizon. At most one transfer is allowed
per GW in this version.

## Search method

Exact enumeration of every three-GW transfer path across the full player pool grows too quickly.
The planner first keeps the strongest horizon-xPts candidates per position, then uses a bounded
beam. A future-hold heuristic helps preserve moves whose value arrives in GW+1/GW+2.

Every retained state has exact rule and score calculations, but pruning means the selected plan is
not guaranteed to be the global optimum. Output records the beam width, candidate cap, alternatives,
coverage exclusions, and this limitation.

Before operational adoption, the search policy needs a walk-forward backtest against transparent
comparators such as no-transfer, the single-GW recommender, and a simple fixture-horizon heuristic.
Only after that assessment should a separate planner protocol be frozen.
