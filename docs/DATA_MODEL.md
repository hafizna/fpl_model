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
