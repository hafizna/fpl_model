# SofaScore local-access probe — 2026-08-16

## Scope

This note records the reproducible local probe for Chelsea team ID `38`. SofaScore remains an
optional experimental source and is not required by the projection model.

## Commands and observations

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe scripts\sofascore_spike.py --date 2026-08-15 --team-id 38
```

- Baseline result before changes: `11 passed`.
- Before VPN activation, the configured `www.sofascore.com/api/v1` request failed TLS hostname
  verification. A separate verified request to `https://api.sofascore.com/api/v1` failed the same
  way.
- Local DNS resolved both SofaScore hostnames through `filter.citra.net.id` to `202.65.113.54`.
  The unrelated certificate is therefore consistent with network-level filtering. TLS verification
  was not disabled.
- A user-supplied Chrome screenshot independently showed `NET::ERR_CERT_COMMON_NAME_INVALID` and a
  `*.citra.net.id` certificate before VPN activation. The unsafe browser override was not used.

After VPN activation, the same probe was repeated:

```powershell
Resolve-DnsName api.sofascore.com -Type A
.\.venv\Scripts\python.exe scripts\sofascore_spike.py --date 2026-08-15 --team-id 38
.\.venv\Scripts\python.exe scripts\sofascore_spike.py --date 2026-08-15 --team-id 38 `
  --base-url https://www.sofascore.com/api/v1
```

- DNS now resolved both hostnames through `sofascore.map.fastly.net` to `146.75.27.52`, so the VPN
  removed the Citra DNS interception.
- Both verified direct requests returned HTTP `403`.
- No in-app or attached browser session was available to the agent on either attempt. The user
  subsequently confirmed that the normal website loads locally after VPN activation through
  Cloudflare's browser verification. This supports a browser-versus-direct-client access-control
  difference, but does not authorize automating or bypassing that verification.
- The user then opened the scheduled-events API URL in the normal browser and received
  `{"error":{"code":403,"reason":"challenge"}}`. Public website access therefore does not make
  the structured endpoint available to this research workflow.

Because no provider payload was retrieved, Chelsea events, lineups, average positions, and
heatmaps were not available. Coordinate orientation remains **unresolved**; no flip setting or
tactical-role conclusion should be inferred from this probe.

## LanusStats comparison

LanusStats currently keeps one Chrome session open, navigates that browser to SofaScore `api/v1`
URLs, and parses the JSON text from the rendered page. Its average-position method still uses one
match-level request before player heatmaps, which matches this project's preferred request order.

Its implementation also uses an anti-detection driver and randomized user agents. Those mechanisms
are outside this project's responsible-use rules and must not be copied.

## Browser-backed adapter decision

Do not implement a browser-backed adapter while a standard, user-verified browser still receives a
challenge response from the structured endpoint. Handling or imitating that challenge would be an
anti-bot bypass, not a maintainable transport.

Reconsider an optional sibling transport only if an authorized browser session can retrieve the
JSON without an unresolved challenge, or SofaScore provides a sanctioned access route. In that
case, the smallest design is:

1. Reuse one standard Playwright or Selenium browser context supplied by the caller.
2. Navigate normally to the same `api/v1` URL and parse the visible JSON response body.
3. Require the user to complete any provider verification interactively. Stop on unresolved
   access-denied, challenge, or consent pages; do not rotate identities, patch drivers, solve
   challenges automatically, extract browser cookies, or alter TLS verification.
4. Cache raw JSON locally with request URL and retrieval timestamp for reproducibility.
5. Feed payloads into the existing provider-specific flattening methods, attempting scheduled
   events, lineups, and match-level average positions before selected player heatmaps.
6. Keep the browser package in an optional dependency group and out of all baseline/backtest paths.

Before normalising any attacking axis, compare several unmistakable roles from both home and away
sides (goalkeepers, centre-forwards, and wide defenders) and corroborate with selected heatmaps.
Persist the evidence and chosen per-side transform; do not infer orientation from field names alone.
