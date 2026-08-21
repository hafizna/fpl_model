# Empirical projection priors

Baseline policy v7 fills supported preseason projection gaps without treating missing history as
zero ability. It derives priors only from the baseline's pinned player-rate, appearance,
team-strength, and official-player snapshots.

## Player-rate prior

For a player with no usable previous-Premier-League rate, the pipeline uses component-wise medians
from current players with usable history in the same FPL position and price band. Cheap bands are
GK/DEF at most GBP 4.5m and MID/FWD at most GBP 5.5m. A rate prior requires at least ten comparable
players.

The synthetic reference window is 900 minutes and ten starts. Only rates are transferred; the
target player's own appearance probabilities and fixture/team context still determine xPts.

## Appearance prior

When availability is resolved but playing-time history is missing, the pipeline first looks for at
least five comparable players with the same position, price band, and causal gap cohort:

- promoted player without a previous-PL rate;
- current-only/new-signing player without a previous-PL rate;
- returning or unproven player with an unusable rate row;
- player with a usable previous-PL rate but missing appearance history.

If that exact cohort is too small, it falls back to the broader position/price band. Medians are
taken over conditional start, substitute, 60-minute, and minutes assumptions, then combined with
the target player's own availability probability.

## Safety and audit flags

No prior is applied to a player carrying `FPL_ROSTER_BLOCKED`, with unresolved availability, with
no fixture, or when the minimum comparison population is unavailable. Applied priors carry flags
including `EMPIRICAL_PLAYER_RATE_PRIOR`, `EMPIRICAL_APPEARANCE_PRIOR`, their cohort, scope, and
sample size.

On the pinned 2026/27 GW1 snapshot this moves coverage from 404/600 to 583/600. The remaining 17
players are roster-blocked, giving 100% coverage among 561 selectable players.

Coverage is only one safety gate. A retrospective v7 optimizer simulation still benches Watkins
in GW1 and GW2, so the recommender remains `RESEARCH_ONLY` until counterfactual bench economics,
goalkeeper structure, premium usage, calibration, and uncertainty checks pass.
