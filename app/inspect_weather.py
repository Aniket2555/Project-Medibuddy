"""CLI: fetch live weather for a city and print the derived facts.

    python -m app.inspect_weather Bhopal
    python -m app.inspect_weather "New Delhi" --day tomorrow --part afternoon
    python -m app.inspect_weather Bhopal --save evals/fixtures/live_bhopal.json
"""

from __future__ import annotations

import argparse
import json
import sys

from dotenv import load_dotenv

from app.facts import TimeWindow, derive_facts, format_facts
from app.weather import OpenMeteoClient, WeatherDataError


def main() -> int:
    load_dotenv()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # so "°C" prints on Windows consoles
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("city")
    p.add_argument("--day", default="today", choices=["today", "tomorrow", "day_after"])
    p.add_argument("--part", default="now", choices=["now", "morning", "afternoon", "evening", "night", "whole_day"])
    p.add_argument("--save", help="write the raw forecast JSON (plus location) to this path")
    args = p.parse_args()

    client = OpenMeteoClient()
    try:
        loc = client.geocode(args.city)
        raw = client.forecast(loc)
        facts = derive_facts(raw, TimeWindow(args.day, args.part), loc.label)
    except WeatherDataError as exc:
        print(f"[{type(exc).__name__}] {exc.user_message} ({exc})", file=sys.stderr)
        return 1

    print(f"{loc.label}  ({loc.latitude}, {loc.longitude})  window: {facts['window_label']}\n")
    print(format_facts(facts))
    if args.save:
        payload = {"_location": loc.__dict__, **raw}
        with open(args.save, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=1)
        print(f"\nsaved raw forecast -> {args.save}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
