# Canonical data model

The project separates **identity**, **facts**, **context**, and **projections**. This is intentional: a player being registered as a defender is not the same thing as that player's tactical role in a specific match.

## Core entities

### Player identity

Stable-ish provider-independent fields:

- `fpl_id`
- `player_code`
- player name
- `team_id`
- `fpl_position`
- `season`

Provider IDs (Understat, spatial provider, etc.) will live in an explicit bridge table rather than being inferred from names during modelling.

### Match identity

- `match_id`
- date / kickoff
- competition
- season
- team / opponent
- home-away
- optional Premier League GW

This allows preseason, World Cup, Champions League, cups, and Premier League matches to coexist without pretending they are equivalent samples.

## Preseason availability fact

One row per player-match appearance:

- started
- minutes
- sub-on / sub-off minute
- nominal position
- nominal formation
- manager

This table primarily informs availability, start probability, and expected minutes.

## Spatial fact

Provider coordinates are normalised to a 0..1 pitch with the attacking direction on +x. Per player-match we derive:

- average x/y
- final-third share
- penalty-box share
- left / centre / right lane share
- relative height versus a team or role reference
- exploratory Role Attack Index

The Role Attack Index is **not yet a projection multiplier**. It is a diagnostic until historical calibration demonstrates predictive value.

## Tactical role

Formation strings are stored as metadata. Tactical roles are represented by continuous features such as:

- width
- height
- centrality
- build-up involvement
- box presence
- defensive load

This permits two players both labelled `RB` to have very different FPL priors, for example a high wing-back versus an inverted full-back.

## Context fact

Context is recorded at the player-GW or team-GW level and remains descriptive until calibrated:

- manager tenure / regime change
- promotion prior
- tournament minutes / return-to-club dates
- preseason minutes
- rest days
- minutes and matches in last 7/14 days
- European/cup proximity

### Availability and eligibility resolution

Official FPL player state is retained as a timestamped raw snapshot. A separate resolution run
selects only a snapshot captured no later than the target deadline and converts it into the causal
`availability_probability` input used by the appearance model.

The first policy is deliberately conservative:

- an explicit deadline-relevant FPL chance is divided by 100;
- status `available` with a blank chance resolves to 1.0;
- suspended, unavailable, non-selectable, or removed players are eligibility-blocked at 0.0;
- any other missing probability remains `NULL` with a data-quality flag;
- injury-news prose is retained but never parsed into an invented probability;
- a reviewed override must identify its source, rationale, observation time, target GW, and expiry.

The resolution is not start probability and does not directly multiply xPts. It becomes one input
to mutually exclusive start, substitute-appearance, and absence scenarios. Resolution rows and the
source snapshot remain immutable for later deadline-safe backtesting.

### Appearance-history import boundary

Vaastav's per-GW rows can identify starts, substitute appearances, and played minutes, but a
zero-minute row does not reveal whether the player was an unused matchday substitute or absent from
the squad. Those states cannot be conflated because the Benchwarmers appearance prior explicitly
uses `unused_substitute / squad_selections`.

This was checked against the extracted workbook cases: Vaastav has one zero-minute row for Raya
while the workbook has zero unused-sub appearances, and 12 for Chiesa while the workbook has 10.
Promoted-team players may have no prior Premier League rows at all. The preseason pipeline therefore
imports the workbook's already-resolved player-code keyed fields through a strict CSV boundary:

- starts;
- substitute appearances;
- unused substitute appearances;
- minutes per start;
- minutes per substitute.

Missing history remains missing; it is not converted to zero history. The initial materialiser is
restricted to GW1, where the extracted workbook uses 100% previous-season weight. Extending this to
GW2+ requires a separate deadline-safe current-season squad-status source and explicit blend rules.

Players added after the workbook snapshot may instead receive a reviewed conditional appearance
scenario. It records `P(start | available)`, `P(substitute cameo | available)`,
`P(60+ | start)`, and mean minutes for starts and cameos, together with a source, rationale,
observation timestamp, target gameweek, and optional expiry. Availability remains an upstream
input: for example, 50% availability scales the scenario probabilities rather than multiplying
component xPts directly.

### Player-fixture rate-history boundary

Previous-season xG, xA, saves, cards, bonus, BPS, and defensive contributions are imported from a
pinned Vaastav Git revision. Each canonical fact is keyed by stable player code and fixture ID and
retains its gameweek and kickoff. Exact full-row duplicates are removed; conflicting duplicates or
season totals that do not reconcile to `players_raw.csv` are rejected.

The preseason rate materialisation stores the workbook's prior-season windows explicitly:

- long form: GW1--38;
- attacking short form: GW33--38 (six gameweeks);
- DefCon short form: GW29--38 (ten gameweeks).

Window inputs remain raw totals and minutes. Conversion to per-start rates and application of
appearance probabilities happen later in the component model. A current player without linked
Premier League history remains missing pending an explicit promoted/new/returning-player prior.

## Projection fact

The projection table will contain component-level expected points, not only a final number:

- appearance
- 60+ minutes
- goals
- assists
- clean sheets
- goals conceded
- saves
- cards
- bonus
- defensive contributions
- fixture/home-away adjustment
- final xPts
- uncertainty/confidence

This makes every difference from the Benchwarmers baseline auditable.

### Appearance/minutes foundation

The read-only Benchwarmers extraction in
`docs/research/benchwarmers_appearance_reference.json` provides formula references and five golden
cases for the appearance/start/minutes block. The Python baseline retains the workbook's historical
appearance prior, zero-history floor, seasonal blend weights, and minutes-per-start 60-minute curve.
It also accepts a complete set of mutually exclusive player-fixture minute scenarios, including the
zero-minute outcome, and derives:

- start probability
- substitute-appearance probability
- appearance probability
- 60-minute probability
- expected minutes
- one-point appearance xPts
- additional 60-minute xPts

This preserves the non-linearity of the 60-minute scoring threshold. Later context features may
change scenario probabilities through calibrated logic; they must not multiply the resulting xPts.

The translation deliberately distinguishes the workbook's conditional `MODEL!2` value,
`P(60+ | start)`, from the unconditional probability that earns the second appearance point:
`P(start) * P(60+ | start)`. It also reconciles the workbook's capped appearance prior with its raw
start rate so that `P(start) <= P(appearance)`.

Known spreadsheet wiring quirks are research references, not baseline behavior. In particular, the
Python component does not copy the `T1` manual-start point boost or the double application of the
home/away multiplier on the non-start branch. Those choices are locked by golden tests so that any
future exact-compatibility mode would need to be explicit and isolated.

### Goal/assist foundation

The goal/assist reference extraction lives in
`docs/research/benchwarmers_goals_assists_reference.json`. The Python component preserves the live
workbook path:

1. convert long-form and short-form xG/xA totals to per-90 rates using minutes actually played;
2. rescale those rates by the expected fraction of a match played when starting;
3. blend the long and short windows;
4. apply the opponent's defensive xGC rate relative to league average;
5. convert contributions to FPL points, including positional goal scoring and the explicit
   workbook assist boost.

The rate stage remains conditional on starting and contains no appearance probability. A separate
exposure step converts it to unconditional xPts using
`P(start) + P(substitute appearance) * substitute/start minutes ratio`.

This deliberately differs from the spreadsheet's final not-start branch, which uses
`1 - P(start)` and therefore treats genuine absences as substitute cameos. Home/away adjustment is
also excluded from this component so it can be introduced once, in the dedicated fixture layer,
instead of inheriting the spreadsheet's double-application bug.

### Clean-sheet/goals-conceded foundation

The defensive reference extraction lives in
`docs/research/benchwarmers_clean_sheets_goals_conceded_reference.json`. The live workbook path
blends team long/short-form xGC, applies its linear Understat correction, scales that rate by the
opponent's attacking xG relative to league average, and treats the result as a Poisson lambda.

The Python component retains that rate path and exposes both workbook probabilities:

- `P(clean sheet) = exp(-lambda)`
- `P(2+ goals conceded) = 1 - exp(-lambda) * (1 + lambda)`

Player exposure is made explicit. Clean-sheet xPts is multiplied by the unconditional probability
of playing at least 60 minutes. Goals-conceded exposure uses mutually exclusive start and substitute
appearance probabilities; the substitute lambda is rescaled by the cameo/start minutes ratio.
Absence probability contributes to neither branch.

The workbook applies at most one goals-conceded deduction via `-P(2+ goals conceded)`. Official FPL
scoring deducts one point **for every** two goals conceded by a goalkeeper or defender. The coherent
projection therefore uses `E[floor(goals conceded / 2)]` under the Poisson distribution, while
retaining the workbook approximation as a separately named diagnostic for golden parity. See the
[official scoring rules](https://fplchallenge.premierleague.com/help/rules).

### Saves/cards/bonus/DefCon foundation

The final component-family extraction lives in
`docs/research/benchwarmers_saves_cards_bonus_defcon_reference.json`. The Python translation keeps
the workbook calculations available as explicit diagnostics and corrects only well-defined scoring
or exposure issues:

- saves retain the workbook's opponent-attacking-strength adjustment and continuous `saves / 3`
  value for golden parity; the projection path models save counts as Poisson and evaluates
  `E[floor(saves / 3)]`;
- yellow and red-card rates use the official deductions, while `workbook_red_card_xpts_if_start`
  records the spreadsheet's all-zero red-card branch;
- bonus retains the five-start BPS fallback, season blend, and position-dependent fixture signal,
  then bounds the match expectation to the valid 0--3 interval;
- DefCon uses the workbook's Poisson threshold model: 10 contributions for defenders and 12 for
  midfielders/forwards, worth two points, with goalkeepers ineligible.

As in the attacking and defensive components, appearance exposure is separate. Linear events use
the mutually exclusive start and substitute-appearance probabilities. Saves and DefCon recompute
their nonlinear bundle/threshold probabilities at substitute minutes rather than treating every
non-start as either a full absence or a fixed cameo.

The workbook does not model penalty saves, penalty misses, or own goals. Those remain explicit
coverage gaps; no unsupported prior is fabricated. Historical bonus/BPS is also tagged as a
cross-regime prior because the official 2026/27 BPS rules changed. See the
[official scoring rules](https://fplchallenge.premierleague.com/help/rules) and
[2026/27 bonus-system changes](https://www.premierleague.com/en/news/4679946/whats-new-in-202627-fantasy-changes-to-bonus-points-system).

### Fixture and home/away foundation

The fixture reference extraction lives in
`docs/research/benchwarmers_fixture_home_away_reference.json`. The spreadsheet creates two slots
for every player in every GW, encodes venue through opponent-code letter case, and turns unused
slots into synthetic blank rows. The Python model instead represents each scheduled match as an
explicit `FixtureContext` containing fixture ID, GW, slot, team, opponent, venue, and kickoff:

- a normal GW has one fixture record;
- a double GW retains two independently projected records and sums them only during aggregation;
- a blank GW has no fixture record, rather than a row whose intermediate values must be guarded;
- fixtures without an assigned FPL `event` are omitted until the provider assigns a GW;
- more than two fixtures are retained instead of inheriting the workbook's two-slot limit.

Fixture strength remains component-specific. `FixtureStrength` exposes the opponent attack ratio
used by saves and defensive Poisson lambda, the opponent defensive-weakness ratio used by goals,
assists, and attacking bonus, and the workbook's separate inverted signal used for goalkeeper/
defender bonus. Cards and DefCon receive no invented opponent multiplier because the live workbook
has none.

The extracted global home/away scalar is applied exactly once to the already exposure-weighted
fixture total. A separate `project_workbook_fixture_totals` diagnostic reproduces `BA`, `BG`, and
`BI`, including the proven `H/A²` treatment of the not-start branch, and reports the difference
from a single-application calculation. This keeps spreadsheet parity testable without carrying the
double application into the coherent projection path.

For walk-forward backtests, fixture records and GW assignments must come from a deadline-time
snapshot. The current workbook contains one match whose distant kickoff remains tagged to GW2;
the Python conversion preserves provider assignments visibly rather than silently rewriting them.

### End-to-end baseline projection

`BaselineComponentProjections` composes the independently tested appearance, attacking, defensive,
saves, discipline, bonus, and DefCon outputs into the eleven canonical `ScoringComponents`. These
values are already unconditional: start, substitute-appearance, 60-minute, and absence exposure has
been resolved inside the appropriate component. The composer performs no new rate calculation and
does not reapply appearance probability. It applies the fixture's home/away scalar once and returns
both the component breakdown and final xPts.

### Walk-forward backtest fact

One `BacktestObservation` represents one historical player-fixture prediction and records:

- season, GW, player, and fixture identity;
- deadline and fixture kickoff;
- the newest timestamp used by any feature (`feature_cutoff`);
- when the realised outcome became available for later training/calibration;
- predicted xPts and actual FPL points.

The validation layer rejects features newer than the deadline, naive timestamps, post-kickoff
deadlines, and duplicate player-fixture predictions. For each evaluation deadline, its training
fold includes only earlier predictions whose outcomes were already available. This matters for
postponed fixtures: an old GW label alone never makes a future result eligible for training.

These are backtest primitives, not a completed benchmark run. Historical input snapshots still
need to be materialised so every player, fixture assignment, availability flag, and team-strength
estimate reflects what was knowable at that deadline.

### Historical materialisation smoke test

Vaastav's 2025/26 `merged_gw.csv` supplies complete realised player-fixture outcomes but not
archived FPL deadlines or the Benchwarmers inputs as they stood at each deadline. The historical
materialiser therefore supports a deliberately limited pipeline test:

1. infer each GW deadline as its earliest kickoff minus an explicit 90-minute buffer;
2. treat an outcome as available three hours after its fixture kickoff;
3. predict each player's next row using only their mean points from outcomes already available;
4. retain zero-minute rows, because filtering on realised minutes would use future information;
5. remove only byte-for-byte-equivalent table rows and reject conflicting duplicate identities.

The first run covered 38 folds and 29,057 evaluation rows from GW2 onward. Its expanding-mean
smoke baseline produced MAE `1.0565` and RMSE `2.0552`. These values validate the mechanics only;
they are not evidence about Benchwarmers accuracy. The upstream `xP` column is also not used because
this dataset does not establish when each value was captured relative to its deadline.

The exact run metadata, source-frame hash, duplicate count, metrics, and reproduction command are
stored in `docs/research/walk_forward_smoke_2025_26.json`. A genuine model benchmark remains gated
on reconstructing or obtaining deadline-time appearance, rate, team-strength, and fixture inputs.

### Historical snapshot provenance

Repository history can recover some deadline-time state without selecting a future file version.
For each deadline, the Vaastav adapter lists commits that touched the season's `players_raw.csv`
and selects only the newest revision whose commit timestamp is at or before the cutoff. CSV content
is then fetched from that pinned SHA rather than from the mutable `master` branch.

The 2025/26 coverage audit found 12 revisions and a causal snapshot for all 38 inferred deadlines,
but only 13 GWs had a snapshot no older than 14 days. Median age was 27.5 days and maximum age was
91.6 days. A stale snapshot is still causal, but it is not adequate evidence for current injury or
availability state.

Other coverage gaps prevent an honest claim of exact Benchwarmers backtest parity:

- stable player codes link only 534 of 841 target-season players to 2024/25;
- prior-season DefCon, CBI, recoveries, and tackles are absent;
- historical Understat snapshots used by the workbook are not present;
- fixture assignments and official deadlines are inferred from final files, not archived snapshots.

The machine-readable audit is stored in
`docs/research/vaastav_snapshot_coverage_2025_26.json`. Until these gaps are resolved, the project
will keep the walk-forward roadmap item open and will not publish a reconstructed score as if it
were the real Benchwarmers baseline.
