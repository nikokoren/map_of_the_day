#!/usr/bin/env python3
"""
A frozen feed for the marketplace screenshot.

The plugin listing needs a picture of the recipe looking its best, which
means a real render of a hand-picked map rather than whatever today
happens to serve. This writes showcase.json in exactly the shape
today.json has, so the recipe renders it without knowing the difference
-- point the plugin's polling URL at it, take the screenshot, point it
back.

    python3 showcase.py --id 2006627265
    python3 showcase.py --id 2021668695   # the 1575 view of Algiers
"""

import argparse
import json
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import daily  # noqa: E402

OUT = os.path.join(HERE, "showcase.json")

# Georg Braun's view of Algiers, Cologne 1575 -- ships in the harbour, a
# figure in the foreground, the city walls drawn in elevation. Chosen
# over 1,279 measured candidates by rendering the shortlist at panel
# size and 2-bit depth and reading the captions it would produce.
#
# Eckebrecht's 1630 double-hemisphere world map (2006627265) is the
# better picture and loses on words: its description says the sheet
# "appears to be a later reprinting", which is not what a shop window
# wants. Algiers gets "Shown here is one of the earliest printed maps of
# the city of Algiers", which is.
DEFAULT_HERO = "2021668695"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", default=DEFAULT_HERO, help="LOC item number")
    ap.add_argument("--date", help="YYYY-MM-DD shown in the feed")
    args = ap.parse_args()

    pool = daily.load_pool()
    entry = next((e for e in pool["maps"] if e["id"] == args.id), None)
    if entry is None:
        raise SystemExit(f"{args.id} is not in the pool")

    day = date.fromisoformat(args.date) if args.date else date.today()
    payload = daily.build_payload(entry, "all", day, pool, "showcase", 0)
    payload["topic_size"] = pool["count"]
    slim = daily.slim(payload)

    # Every cell gets the same map, so the screenshot looks the same
    # whatever the settings happen to be, and every day carries it too,
    # so the local-day lookup in the markup always lands.
    # One topic, not forty-eight. The markup falls back to "all" when a
    # reader's exact combination is missing, so a feed carrying only that
    # renders the same map for every possible setting -- which is what a
    # screenshot wants, and keeps the file small.
    keys = ["all"]
    picks = {"all": slim}
    index = daily.day_index(day)

    feed = {
        "date": day.isoformat(),
        "day_index": index,
        "default_day": str(index),
        "generated": payload["generated"],
        "pool_size": pool["count"],
        "pick_fields": list(daily.PICK_FIELDS),
        "theme_options": [{"key": k, "label": daily.TOPIC_LABELS[k],
                           "size": pool["count"]} for k in keys
                          if not k.startswith("era-")],
        "era_options": [{"key": s, "label": l, "size": pool["count"]}
                        for s, l, _, _ in daily.ERAS],
        "cells": [], "cell_keys": [],
        "keys_by_label": daily.label_aliases(
            [k for k in keys], {k: payload for k in keys}),
        "heading": "MAP OF THE DAY",
        "source": daily.CREDIT,
        "credit": "Library of Congress",
        "rights": daily.RIGHTS,
        "item_url_prefix": "https://www.loc.gov/item/",
        "days": {str(index + shift): picks for shift in daily.DAY_SPAN},
    }
    with open(OUT, "w") as fh:
        json.dump(feed, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print("wrote showcase.json")
    for name, value in zip(daily.PICK_FIELDS, slim):
        print(f"  {name:18s} {str(value)[:88]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
