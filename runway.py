#!/usr/bin/env python3
"""
How many days of reviewed queue are left, and whether to say so.

`reviewed_to` in curation.json is the last day a person actually looked
at; review.json says how far the queue now reaches. The gap between
today and reviewed_to is the runway -- the number of days that will
reach a screen having been seen by someone first.

Prints a line for the workflow to read. Exit 0 always: a reminder that
cannot be computed is not a reason to fail a build.
"""
import json, os, sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
WARN_AT = 5          # days of runway left before it is worth saying


def load(name):
    try:
        with open(os.path.join(HERE, name)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def main():
    curation, review = load("curation.json"), load("review.json")
    today = date.today()

    reviewed_to = curation.get("reviewed_to")
    runway = None
    if reviewed_to:
        try:
            runway = (date.fromisoformat(reviewed_to) - today).days
        except ValueError:
            runway = None

    queue_to = review.get("to") or ""
    waiting = len(review.get("items") or [])

    print("runway={}".format("" if runway is None else runway))
    print("reviewed_to={}".format(reviewed_to or ""))
    print("queue_to={}".format(queue_to))
    print("waiting={}".format(waiting))
    # Nothing reviewed at all is worth saying; so is a runway running out.
    print("due={}".format(
        "yes" if (runway is None or runway <= WARN_AT) and waiting else "no"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
