#!/usr/bin/env python3
"""
Picks the map of the day and writes the JSON files TRMNL polls.

Reads pool.json (built by harvest.py) and writes map.json plus one file
per category under today/. The pick is a pure function of
the pool and the date: the same day always yields the same map, so a
device that polls at 07:00 and again at 19:00 sees the same thing, and
re-running this script never reshuffles the screen.

Run locally with:
    python3 map_of_the_day/daily.py
    python3 map_of_the_day/daily.py --date 2026-12-25 --dry-run
    python3 map_of_the_day/daily.py --preview 7     # the next week's picks
"""

import argparse
import hashlib
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

# ============================================================
# config
# ============================================================

HERE = os.path.dirname(os.path.abspath(__file__))
POOL_PATH = os.path.join(HERE, "pool.json")
TODAY_DIR = os.path.join(HERE, "today")
DEFAULT_PATH = os.path.join(HERE, "map.json")   # the "all categories" feed

UA = "mission-control-trmnl/1.0 (github.com/nikokoren/mission_control)"

# Mixed into every hash. Changing it reshuffles the whole schedule, which
# is occasionally useful and otherwise should be left alone.
SALT = "mission-control/map-of-the-day/v1"

# The panels this has to look right on. TRMNL's framework gives the OG an
# 800x480 viewport and the newer, larger panel a 1040x780 one -- but that
# is the CSS box, and the physical panel behind it is 1872x1404 at 227
# ppi. An image meant for it has to carry the panel's pixels, not the
# box's, or it renders soft.
#
# So the payload ships a URL per panel and, more importantly, the IIIF
# base to build any other: markup that knows its own screen can ask for
# exactly what it needs (see image_base in the README). The default
# `image` is the largest, because the picture is fetched by whatever
# renders the markup, not by the battery-powered device -- so paying for
# pixels costs the screen nothing.
OG_BOX = (800, 480)
X_BOX = (1872, 1404)
DEFAULT_BOX = X_BOX

# The size the ink measurement is taken at. Not a display size: a fixed
# yardstick, so the byte threshold below means the same thing for every
# map regardless of which panel ends up showing it.
PROBE_BOX = (800, 480)

# A candidate whose image is definitively gone -- or too sparse to be
# worth a day of screen time -- is skipped and the next one in the day's
# order takes its place. Anything less certain than that is not allowed
# to change the pick.
MAX_SKIPS = 4
DEAD_CODES = (403, 404, 410, 451)
CHECK_TIMEOUT = 12

# How many bytes the map has to weigh at PROBE_BOX. At a fixed size the
# file size is a direct measure of how much ink is on the map: hand-drawn
# plats of four blocks come back at 16-29KB, engraved city views and
# railroad maps at 50-80KB. Cheaper and more honest than any metadata
# field, because it measures the picture itself.
MIN_INK_BYTES = 32_000

# Total seconds all the image checks together may spend. The image
# service usually answers a HEAD in under a second but can take six or
# more when it has to render the derivative first, so the budget is
# generous; past it the remaining categories are written unchecked
# rather than letting the job hang.
CHECK_BUDGET = 240

CREDIT = "Library of Congress, Geography and Map Division"
RIGHTS = "No known restrictions on publication"

CATEGORY_LABELS = {
    "all": "Maps",
    "cities": "Cities & Towns",
    "exploration": "Discovery & Exploration",
    "military": "Battles & Campaigns",
    "nature": "National Parks",
    "panoramas": "Panoramic Views",
    "railways": "Railroads",
}

EPOCH = date(1970, 1, 1)


# ============================================================
# selection
# ============================================================

def day_index(day):
    """Days since the epoch. The one number the whole schedule turns on."""
    return (day - EPOCH).days


def order_for(entries, category, cycle):
    """
    The order this category's maps are shown in during one pass through
    the pool. Sorting by a hash of the id gives a shuffle that is stable
    (same inputs, same order, on any machine and in any Python) without
    storing a schedule anywhere. The cycle number is in the hash, so the
    next pass through the pool comes out in a different order.
    """
    def key(entry):
        seed = "{}|{}|{}|{}".format(SALT, category, cycle, entry["id"])
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return sorted(entries, key=key)


def candidates_for(entries, category, day):
    """
    The day's pick first, then the maps that stand in for it if its image
    turns out to be gone.
    """
    index = day_index(day)
    total = len(entries)
    cycle, position = divmod(index, total)
    ordered = order_for(entries, category, cycle)
    return [ordered[(position + offset) % total]
            for offset in range(min(MAX_SKIPS + 1, total))]


# ============================================================
# images
# ============================================================

def iiif_base(service):
    return "https://tile.loc.gov/image-services/iiif/" + service


def iiif(service, box, quality="gray"):
    """
    One image URL. "!w,h" means "fit inside this box", so the map keeps
    its proportions whatever box it is given.
    """
    return "{}/full/!{},{}/0/{}.jpg".format(
        iiif_base(service), box[0], box[1], quality)


def image_urls(entry):
    """
    The Library's IIIF service does the resizing and the greyscale
    conversion, so a size is just a different URL -- which is why the
    base is in the payload too.
    """
    return {
        "image": iiif(entry["s"], DEFAULT_BOX),
        "image_og": iiif(entry["s"], OG_BOX),
        "image_x": iiif(entry["s"], X_BOX),
        "image_color": iiif(entry["s"], DEFAULT_BOX, "default"),
        "thumb": iiif(entry["s"], (320, 320)),
        "probe": iiif(entry["s"], PROBE_BOX),
        "image_base": iiif_base(entry["s"]),
    }


_budget_started = [None]


def budget_left():
    """Seconds of image checking still allowed this run."""
    if _budget_started[0] is None:
        _budget_started[0] = time.monotonic()
    return CHECK_BUDGET - (time.monotonic() - _budget_started[0])


def image_state(url):
    """
    ('ok' | 'thin' | 'dead' | 'unknown', bytes). Only 'dead' and 'thin'
    move the pick; a timeout or a 500 leaves the day's map exactly where
    it was, which is the difference between a bad minute at LOC and a
    different map. A HEAD is enough -- the image service reports the
    rendered size without sending the picture.
    """
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT) as resp:
            if resp.status >= 400:
                return "unknown", 0
            size = int(resp.headers.get("Content-Length") or 0)
            if not size:
                return "unknown", 0
            return ("ok" if size >= MIN_INK_BYTES else "thin"), size
    except urllib.error.HTTPError as e:
        return ("dead" if e.code in DEAD_CODES else "unknown"), 0
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
            ConnectionError, OSError, ValueError):
        return "unknown", 0


# ============================================================
# payload
# ============================================================

def title_line(entry):
    """Title trimmed to something that fits a headline without wrapping
    off the screen. The full title stays available as `title`."""
    title = entry["t"]
    if len(title) <= 64:
        return title
    # Prefer cutting at the subtitle marker LOC uses before chopping words.
    for marker in (" : ", "; ", ", showing", " -- "):
        if marker in title[:64]:
            return title.split(marker)[0].strip(" ,:;-")
    return title[:61].rsplit(" ", 1)[0].rstrip(" ,;:.-") + "..."


def build_payload(entry, category, day, pool, checked, ink_bytes=0):
    aspect = round(entry["w"] / float(entry["h"]), 3)
    urls = image_urls(entry)
    creator = entry.get("c") or ""
    place = entry.get("p") or ""

    payload = {
        "date": day.isoformat(),
        "category": category,
        "category_label": CATEGORY_LABELS.get(category, category.title()),
        "heading": "MAP OF THE DAY",

        "title": entry["t"],
        "title_short": title_line(entry),
        "year": str(entry["y"]),
        "creator": creator,
        "place": place,
        "collection": entry.get("col", ""),
        "description": entry.get("d", ""),
        "medium": entry.get("m", ""),

        # Ready-made lines, for the common case where the layout wants one
        # string under the title rather than four fields to arrange.
        "byline": " - ".join(p for p in (creator, str(entry["y"])) if p),
        "subtitle": " - ".join(p for p in (place, entry.get("col", "")) if p),

        "image": urls["image"],
        "image_og": urls["image_og"],
        "image_x": urls["image_x"],
        "image_color": urls["image_color"],
        "thumb": urls["thumb"],
        # Build any other size from this: <base>/full/!w,h/0/gray.jpg
        "image_base": urls["image_base"],
        "image_width": entry["w"],
        "image_height": entry["h"],
        "aspect": aspect,
        "orientation": "landscape" if aspect >= 1.0 else "portrait",

        "source": CREDIT,
        "rights": RIGHTS,
        "credit": "Library of Congress",
        "item_url": "https://www.loc.gov/item/{}/".format(entry["id"]),
        "item_id": entry["id"],

        "pool_size": pool["count"],
        "pool_generated": pool.get("generated", ""),
        "image_checked": checked,
        "ink_bytes": ink_bytes,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return payload


def pick(entries, category, day, check):
    """The day's map, skipping images that are gone or nearly blank."""
    candidates = candidates_for(entries, category, day)
    if not check or budget_left() <= 0:
        return candidates[0], "skipped", 0
    for entry in candidates:
        if budget_left() <= 0:
            return entry, "skipped", 0
        state, size = image_state(image_urls(entry)["probe"])
        if state in ("ok", "unknown"):
            return entry, state, size
        sys.stderr.write("  {} is {}, trying the next one\n"
                         .format(entry["id"],
                                 "gone" if state == "dead" else
                                 "mostly blank paper ({}KB)".format(size // 1024)))
    # Every stand-in failed too, which says something is wrong at the far
    # end rather than with this particular map. Show the day's map.
    return candidates[0], state, size


# ============================================================
# self-test
# ============================================================

def selftest(entries, day):
    """
    The properties the schedule has to have, checked against the real
    pool rather than a fixture. Worth running before publishing and after
    any change to the selection code.
    """
    failures = []
    total = len(entries)
    index = day_index(day)
    cycle, position = divmod(index, total)

    # 1. The same day gives the same map, every time it is asked.
    if (candidates_for(entries, "all", day)[0]["id"]
            != candidates_for(entries, "all", day)[0]["id"]):
        failures.append("selection is not deterministic")

    # 2. Ids are unique, which is what makes one pass through the pool
    #    show every map exactly once with no repeats.
    ids = [e["id"] for e in entries]
    if len(set(ids)) != total:
        failures.append("pool has {} duplicate ids"
                        .format(total - len(set(ids))))

    # 3. Day n of the cycle really is position n of that cycle's order,
    #    sampled across the whole cycle. (Checking all of them would mean
    #    re-sorting the pool once per day of the cycle.)
    order = [e["id"] for e in order_for(entries, "all", cycle)]
    step = max(1, total // 50)
    for offset in range(0, total, step):
        wanted = day + timedelta(days=offset - position)
        got = candidates_for(entries, "all", wanted)[0]["id"]
        if got != order[offset]:
            failures.append("day {} picked {}, expected {}"
                            .format(wanted.isoformat(), got, order[offset]))
            break

    # 4. The next pass through the pool is in a different order, so the
    #    same map does not come back on the same day every cycle.
    if order == [e["id"] for e in order_for(entries, "all", cycle + 1)]:
        failures.append("the shuffle does not change between cycles")

    # 5. Consecutive days differ. (Only guaranteed inside a cycle: the
    #    map either side of a cycle boundary is drawn from two different
    #    shuffles, so it can coincide once in a pool's worth of days.)
    for offset in (0, 1, 2, total // 3, total - position - 2):
        d = day + timedelta(days=offset)
        if day_index(d) // total != cycle:
            continue
        if (candidates_for(entries, "all", d)[0]["id"]
                == candidates_for(entries, "all", d + timedelta(days=1))[0]["id"]):
            failures.append("same map two days running at " + d.isoformat())

    # 6. Every category is deep enough to be worth offering as a setting.
    for category in sorted({e["k"] for e in entries}):
        count = sum(1 for e in entries if e["k"] == category)
        if count < 60:
            failures.append("category {} has only {} maps"
                            .format(category, count))

    # 7. Every map can produce a payload a template can render.
    pool = {"count": total, "generated": ""}
    for entry in entries:
        payload = build_payload(entry, "all", day, pool, "skipped")
        missing = [f for f in ("title", "title_short", "year", "image",
                               "item_url", "collection")
                   if not payload.get(f)]
        if missing:
            failures.append("{} has empty {}".format(
                entry["id"], ", ".join(missing)))
            break

    for failure in failures:
        print("FAIL: " + failure)
    if not failures:
        print("ok: {} maps, {} categories, no repeats within a cycle of "
              "{} days".format(total, len({e["k"] for e in entries}), total))
    return 1 if failures else 0


# ============================================================
# main
# ============================================================

def load_pool():
    try:
        with open(POOL_PATH) as fh:
            pool = json.load(fh)
    except (OSError, ValueError) as e:
        sys.stderr.write("cannot read {}: {}\n".format(POOL_PATH, e))
        return None
    if not pool.get("maps"):
        sys.stderr.write("pool is empty\n")
        return None
    pool["count"] = len(pool["maps"])
    return pool


def write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
        fh.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="YYYY-MM-DD, defaults to today in UTC")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the picks, write nothing")
    parser.add_argument("--preview", type=int, metavar="N",
                        help="print the next N days of picks and exit")
    parser.add_argument("--no-check", action="store_true",
                        help="skip the image availability check")
    parser.add_argument("--selftest", action="store_true",
                        help="check the schedule's properties and exit")
    args = parser.parse_args()

    pool = load_pool()
    if pool is None:
        # Whatever is already committed stays on screen. A missing pool is
        # a problem for the harvest job, not a reason to blank the plugin.
        return 1

    day = (date.fromisoformat(args.date) if args.date
           else datetime.now(timezone.utc).date())
    entries = pool["maps"]

    if args.selftest:
        return selftest(entries, day)

    if args.preview:
        for offset in range(args.preview):
            d = day + timedelta(days=offset)
            entry = pick(entries, "all", d, check=False)[0]
            print("{}  {:<58} {}".format(d.isoformat(),
                                         title_line(entry), entry["y"]))
        return 0

    categories = ["all"] + sorted({e["k"] for e in entries})
    written = []
    for category in categories:
        subset = entries if category == "all" else [e for e in entries
                                                    if e["k"] == category]
        if not subset:
            continue
        entry, checked, size = pick(subset, category, day,
                                    check=not args.no_check)
        payload = build_payload(entry, category, day, pool, checked, size)
        path = (DEFAULT_PATH if category == "all"
                else os.path.join(TODAY_DIR, category + ".json"))
        print("{:<12} {} ({}) [{}]".format(
            category, payload["title_short"], payload["year"], checked))
        if not args.dry_run:
            write_json(path, payload)
            written.append(os.path.relpath(path, os.getcwd()))

    if written:
        print("wrote " + ", ".join(written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
