# Preseason spatial-data spike

## Why this exists

Nominal formation strings are too coarse for FPL modelling. A player registered as a defender may play as a conservative full-back, an inverted midfielder, or a high wing-back. The spatial layer is intended to quantify those differences.

## Experimental source

SofaScore's web endpoints are undocumented. Recent community-maintained references and Python libraries describe a stable-looking `api/v1` pattern including:

- scheduled football events
- event lineups
- player heatmaps
- player rating-breakdown/event coordinates

The adapter in `src/fpl_model/ingest/sofascore_experimental.py` deliberately treats these endpoints as experimental. They may change or disappear without notice.

Community references used during the spike:

- `https://github.com/federicorabanos/LanusStats`
- `https://github.com/pseudo-r/Public-Sofascore-API`
- `https://github.com/Tariq-15/TacosScore`

These references are not official SofaScore API documentation.

## Candidate endpoint flow

```text
scheduled-events/{date}
        ↓
event ID
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

## Chelsea proof-of-concept

SofaScore's public pages show coverage for several Chelsea 2026 preseason friendlies. The first local smoke test should discover those match IDs through the daily schedule rather than hard-code IDs copied from a webpage.

SofaScore's Chelsea team ID is currently represented as `38` on its public team page. Treat provider IDs as external identifiers, not canonical FPL IDs.

Example:

```bash
python scripts/sofascore_spike.py --date 2026-08-15 --team-id 38
```

Then use one returned event ID:

```bash
python scripts/sofascore_spike.py --event-id EVENT_ID
```

and inspect one player:

```bash
python scripts/sofascore_spike.py --event-id EVENT_ID --player-id PLAYER_ID
```

## Coordinate caveat

Community documentation describes heatmap coordinates on a 0..100 pitch. Even if that remains true, orientation must be verified empirically with players whose tactical location is obvious. Do not infer 'high' versus 'deep' from the axis direction until that check passes.

## What a successful spike produces

For each player-match:

- average pitch height
- average lateral position
- final-third share
- penalty-box share
- left / centre / right lane share
- relative height versus a team reference
- exploratory Role Attack Index

The final model should also retain the raw points or a reproducible cache key so derived features can be regenerated.

## What it does *not* mean

A high Role Attack Index does not automatically mean `xPts * 1.20`. Spatial features should first inform a tactical-role prior and then be validated against future xG/xA/DefCon opportunity and start/minutes outcomes.

## Responsible use

- keep request rates low
- cache local responses during research
- fail gracefully on missing coverage
- isolate this adapter so the main model can run without SofaScore
- re-check provider terms before scaling automated collection
- do not use screenshot OCR as the production fallback
