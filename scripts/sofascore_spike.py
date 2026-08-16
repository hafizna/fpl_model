"""Manual smoke test for the experimental SofaScore spatial adapter.

Examples:

    python scripts/sofascore_spike.py --date 2026-08-15 --team-id 38
    python scripts/sofascore_spike.py --event-id 123456
    python scripts/sofascore_spike.py --event-id 123456 --player-id 78910

This script performs live requests to undocumented provider endpoints. Use it
sparingly and do not run it as a high-frequency scraper.
"""

from __future__ import annotations

import argparse

from fpl_model.ingest.sofascore_experimental import (
    DEFAULT_BASE_URL,
    SofaScoreCoverageError,
    SofaScoreExperimentalClient,
    SofaScoreTransportError,
)
from fpl_model.tactics.spatial import fingerprint


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD scheduled-event discovery date")
    parser.add_argument("--team-id", type=int, help="SofaScore team ID")
    parser.add_argument("--event-id", type=int, help="SofaScore event ID")
    parser.add_argument("--player-id", type=int, help="SofaScore player ID")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"SofaScore API base URL (default: {DEFAULT_BASE_URL})",
    )
    return parser.parse_args()


def run(args: argparse.Namespace) -> None:
    client = SofaScoreExperimentalClient(base_url=args.base_url)
    print(f"SofaScore transport: {client.base_url}")

    if args.date and args.team_id:
        events = client.find_team_events(args.date, args.team_id)
        print(f"Found {len(events)} matching event(s)")
        for event in events:
            home = event.get("homeTeam", {}).get("name")
            away = event.get("awayTeam", {}).get("name")
            print(f"event={event.get('id')} | {home} vs {away}")

    if args.event_id:
        players = client.lineup_players(args.event_id)
        if players.empty:
            print("No lineup players returned")
        else:
            print("\nLineups")
            print(players.to_string(index=False))

        try:
            positions = client.normalised_average_positions(args.event_id)
        except SofaScoreCoverageError as exc:
            print(f"\nAverage positions unavailable: {exc}")
        else:
            print("\nAverage positions (one match-level request)")
            show = [
                "side",
                "sofascore_player_id",
                "player_name",
                "provider_position",
                "normalised_x",
                "normalised_y",
            ]
            print(positions[show].to_string(index=False))

    if args.event_id and args.player_id:
        normalised = client.normalised_player_heatmap(args.event_id, args.player_id)
        result = fingerprint(normalised)
        print("\nDetailed player heatmap fingerprint")
        print(result)

    if not ((args.date and args.team_id) or args.event_id):
        raise SystemExit("Provide --date + --team-id, or --event-id")


def main() -> None:
    args = parse_args()
    try:
        run(args)
    except SofaScoreTransportError as exc:
        raise SystemExit(f"SofaScore transport error: {exc}") from exc


if __name__ == "__main__":
    main()
