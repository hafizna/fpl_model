# In-season rates and five-Gameweek Wildcard planning

The in-season baseline v2 consumes final official xG, xA, defensive contributions
and saves. Previously these updates were materialized but never read by xPts.
It retains the explainable component model and records the exact historical
prior, current-rate run, and final event IDs in `baseline_current_rate_lineage`
and the release manifest. Existing v1 projections are not rewritten.

## Evidence and uncertainty

Only earlier Gameweeks, final and captured by the anchor's `as_of`, enter the
current rate window. Historical source publication/import must also precede the
cutoff. A new official snapshot produces a new immutable release.

The rate posterior uses 900 prior minutes. Players without usable historical
rates blend current observations into the existing empirical position/price
cohort prior; a one-match raw rate is not sent directly to the optimizer. Zero
current minutes preserve the prior. DefCon uses its own historical exposure
denominator. Saves are blended against the baseline's goalkeeper rate.

Both attacking windows receive the same posterior, avoiding a second short-form
weight on the same current sample. xG remains total xG; there is no additional
penalty component to double count. Cards/BPS/bonus and team strength remain
explicit historical priors. Tactical annotations remain diagnostic, and reviewed
appearance scenarios remain available for sourced role changes. Cross-league
rate translation and automatic team-strength adaptation are not implemented.

The changed projection policy remains research/shadow. Old residual/calibration
artifacts are diagnostic references, not validation of v2. This does not alter
the separately frozen appearance-calibration confirmatory protocol.

## Horizon and weekly workflow

`materialize_release.py` and `run_deadline_refresh.py` now default to
`--horizon-length 5`. One to five consecutive GWs are supported, ending no later
than GW38. `project_frozen_horizon.py` also accepts this option. The underlying
projection function keeps its three-GW default for existing Python callers.
Release export, web loading, initial-squad inputs, rating benchmarks, and runtime
checks accept the variable horizon. The legacy `plan_three_gameweeks` wrapper
and its storage/CLI contract still require exactly three.

Future weeks use their own fixtures with inputs frozen at the anchor. Refresh
and review every GW even when the intended action is roll. A five-GW decision
window is not a commitment to ignore new evidence for five weeks.

For a GW4 Wildcard and two saved FT:

| Deadline | Planned action | FT entering | FT available next GW |
|---|---|---:|---:|
| GW4 | Wildcard | 2 | 2 |
| GW5 | Roll | 2 | 3 |
| GW6 | Roll | 3 | 4 |
| GW7 | Roll | 4 | 5 |
| GW8 | Review | 5 | Depends on transfers |

The WC deadline retains banked FT without granting another one. Ordinary
deadlines apply `min(5, max(0, FT - transfers) + 1)` and four points per transfer
beyond the available FT. The [official rules](https://www.premierleague.com/es/news/4661029)
describe saved-FT retention through Wildcard/Free Hit.

## Comparing strategies

The Transfers screen has **Compare Wildcard, hold and FT paths**. Set the desired
published horizon in Settings, actual bank/selling prices and FT in the squad
controls, then choose the number of post-WC GWs to roll (default three).
Clear staged transfers first: the comparison starts from the committed squad.

`POST /api/recommend/wildcard` accepts the existing squad request plus:

```json
{
  "horizon_length": 5,
  "roll_after_wildcard": 3,
  "terminal_ft_value": 0,
  "locked_fpl_ids": [],
  "excluded_fpl_ids": []
}
```

The response contains hold, an ordinary transfer path, and a WC squad/path,
including per-GW XI/captain, bank, hits, and FT transitions. It is pinned to the
release and carries a decision receipt. WC availability is assumed for this
what-if comparison; this endpoint never changes the actual FPL team.

Wildcard affordability uses bank plus actual sale proceeds. Retaining an owned
player uses their sale value as opportunity cost, preserving their real market,
purchase and selling values afterwards. It does not reset the budget to £100m
or charge a sell-and-rebuy spread on retained players.

Ordinary paths search hold, one transfer and atomic two-transfer bundles. A
bundle validates its final budget and club composition, allowing jointly funded
upgrades and club swaps that would fail if checked one transfer at a time.
Cheap funding candidates are included alongside horizon-xPts leaders.

The objective is cumulative lineup/captain xPts minus hits plus an optional
assumed terminal FT value. This defaults to zero and is shown separately from
xPts. Vary it to assess sensitivity; it is not a calibrated forecast. It applies
to FT available after the final horizon deadline, capped at five.

## Practical limits and checks

The planner uses bounded candidate/beam search, with a proxy shortlist for
two-transfer bundles and a WC shortlist evaluated before future transfer paths.
It is not a certified global optimum. It currently supports at most two
ordinary transfers per GW, even when five FT are banked. The future option value
of saving the Wildcard itself is not priced; a small WC gain is not by itself a
recommendation to play the chip. Compare locked captain/keeper/bench structures
through the API when assessing a particular draft.

Lineup scoring enumerates legal formations and their strongest positional
selections exactly. Randomized regression tests compare it against all legal
11-player subsets, including ties and negative points. Integration tests verify
final-only rates actually change xPts, frozen-cutoff identity, bundle funding,
selling-price retention, five-GW FT transitions, and the WC API.
