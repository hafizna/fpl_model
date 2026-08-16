# Preseason spatial-data spike

## Why this exists

Nominal formation strings are too coarse for FPL modelling. A player registered as a defender may play as a conservative full-back, an inverted midfielder, or a high wing-back. The spatial layer is intended to quantify those differences.

## Experimental source

SofaScore's web endpoints are undocumented. Recent community-maintained references and Python libraries describe a stable-looking `api/v1` pattern including:

- scheduled football events
- event lineups
- match-level average positions
- player heatmaps
- player rating-breakdown/event coordinates

The adapter in `src/fpl_model/ingest/sofascore_experimental.py` deliberately treats these endpoints as experimental. They may change or disappear without notice.

Community references used during the spike:

- `https://github.com/federicorabanos/LanusStats`
- `https://github.com/pseudo-r/Public-Sofascore-API`
- `https://github.com/Tariq-15/TacosScore`

These references are not official SofaScore API documentation.

## Recommended spatial hierarchy

For our use case, **match-level average positions should be attempted before per-player heatmaps**.

```text
1. average-positions      one request / match, all players
2. lineups + statistics  role/minutes/context metadata
3. player heatmap        optional deeper evidence for selected players
4. rating-breakdown      optional action-level validation
```

This is much more scalable than downloading a heatmap for every player in every friendly. Average position alone will not capture every inversion/rotation, but it is already enough to distinguish many high-wing-back versus deeper full-back cases. Heatmaps remain useful when the average position is ambiguous.

## Candidate endpoint flow

```text
scheduled-events/{date}
        ↓
event ID
        ↓
event/{event_id}/average-positions
        ↓
all player averageX / averageY
        ↓
relative height / width signal

(optional deeper path)
        ↓
event/{event_id}/lineups
        ↓
SofaScore player ID
        ↓
event/{event_id}/player/{player_id}/heatmap
        ↓
raw x/y coordinates
        ↓
normalise_points(..., x_max=100, y_max=100)
        ↓
SpatialFingerprint
```

## Cloud-runner probe result

A one-off GitHub Actions probe was run on 16 August 2026 against both:

- `https://www.sofascore.com/api/v1`
- `https://api.sofascore.com/api/v1`

using browser-like headers and a deliberately low request rate. Both hosts returned HTTP `403` for scheduled-event requests from GitHub-hosted runner IPs.

This does **not** prove that the endpoint pattern is invalid: current community clients document the same endpoints, and SofaScore is known to apply bot/rate-limit controls. It does mean that cloud CI is not a suitable live ingestion environment for this source. The next acceptance test should run from the user's local machine/network. Do not attempt to bypass provider access controls.

## Chelsea proof-of-concept

SofaScore's public pages show coverage for several Chelsea 2026 preseason friendlies. The first local smoke test should discover those match IDs through the daily schedule rather than hard-code IDs copied from a webpage.

SofaScore's Chelsea team ID is currently represented as `38` on its public team page. Treat provider IDs as external identifiers, not canonical FPL IDs.

Example:

```bash
python scripts/sofascore_spike.py --date 2026-08-15 --team-id 38
```

If that date is not returned by the provider, repeat for known preseason dates such as 1, 5, 8, or 9 August 2026.

Then use one returned event ID. This first prints lineups and tries the one-request match-level average-position endpoint:

```bash
python scripts/sofascore_spike.py --event-id EVENT_ID
```

Only when deeper evidence is useful, inspect one player's full heatmap:

```bash
python scripts/sofascore_spike.py --event-id EVENT_ID --player-id PLAYER_ID
```

## Coordinate caveat

Community documentation describes these spatial coordinates on a 0..100 pitch. Even if that remains true, orientation must be verified empirically with players whose tactical location is obvious. Do not infer 'high' versus 'deep' from the axis direction until that check passes.

## What a successful spike produces

For each player-match we can begin with:

- average pitch height
- average lateral position
- relative height versus teammates / positional peers

For selected player-matches with full heatmap coverage we can add:

- final-third share
- penalty-box share
- left / centre / right lane share
- exploratory Role Attack Index

The final model should retain enough source metadata or local cache information for derived features to be regenerated.

## What it does *not* mean

A high Role Attack Index does not automatically mean `xPts * 1.20`. Spatial features should first inform a tactical-role prior and then be validated against future xG/xA/DefCon opportunity and start/minutes outcomes.

## Responsible use

- keep request rates low
- cache local responses during research
- fail gracefully on missing coverage
- isolate this adapter so the main model can run without SofaScore
- re-check provider terms before scaling automated collection
- respect HTTP access controls; do not build bypass logic
- do not use screenshot OCR as the production fallback
