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
import re
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
DEFAULT_PATH = os.path.join(HERE, "map.json")    # one map, every topic
TOPICS_PATH = os.path.join(HERE, "today.json")   # one map per topic

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
CHECK_BUDGET = 600

CREDIT = "Library of Congress, Geography and Map Division"
RIGHTS = "No known restrictions on publication"

# What a reader can choose to follow. Themes come from the tags the
# harvest attached; eras are read straight off the year, so they need no
# tagging and cannot be wrong.
#
# Each is a selection over the same pool, and every one of them holds
# enough maps not to repeat inside a year.
THEMES = [
    ("all",               "All Maps"),
    ("city-plans",        "City Plans"),
    ("birds-eye-views",   "Bird's-Eye Views"),
    ("civil-war",         "Civil War"),
    ("railroads",         "Railroads"),
    ("roads-and-travel",  "Roads & Travel"),
    ("revolution",        "Revolutionary War"),
    ("land-ownership",    "Land & Property"),
    ("battles-and-forts", "Battles & Forts"),
    ("nautical",          "Nautical Charts"),
    ("exploration",       "Exploration"),
]

ERAS = [
    ("era-1700s",     "The 1700s",   1700, 1799),
    ("era-1800-1849", "1800 - 1849", 1800, 1849),
    ("era-1850-1869", "1850 - 1869", 1850, 1869),
    ("era-1870-1899", "1870 - 1899", 1870, 1899),
    ("era-1900-1929", "1900 - 1929", 1900, 1929),
]

TOPIC_LABELS = dict(THEMES)
TOPIC_LABELS.update({slug: label for slug, label, _, _ in ERAS})

# A theme and an era together -- "a bird's-eye view, from the 1870s".
# Combinations cannot be precomputed one file per selection (ten themes
# and five eras is 2^15 selections), but the *cells* can: a reader
# picking three themes and three eras is choosing among nine of these,
# and the markup rotates over whichever ones exist.
#
# Cells below this many maps are not offered at all. Some are genuinely
# empty and always will be -- there are no 1700s railroad maps, because
# there were no railroads.
CELL_MIN = 25
CELL_SEP = "__"


def cell_key(theme, era):
    return theme + CELL_SEP + era


def in_era(entry, era):
    for slug, _, lo, hi in ERAS:
        if slug == era:
            return lo <= entry["y"] <= hi
    return False


def maps_for(entries, topic):
    """The subset of the pool a topic -- theme, era, or cell -- selects."""
    if topic == "all":
        return entries
    if CELL_SEP in topic:
        theme, era = topic.split(CELL_SEP, 1)
        return [e for e in entries
                if theme in (e.get("g") or []) and in_era(e, era)]
    if topic.startswith("era-"):
        return [e for e in entries if in_era(e, topic)]
    return [e for e in entries if topic in (e.get("g") or [])]

EPOCH = date(1970, 1, 1)

# Fields that change on every run without the map having changed. If
# only these differ, the file is left alone -- otherwise a re-run makes
# a commit that says nothing.
VOLATILE_FIELDS = ("generated", "image_checked", "ink_bytes")


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
_checked = {}


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
    if url in _checked:
        return _checked[url]
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT) as resp:
            if resp.status >= 400:
                return "unknown", 0
            size = int(resp.headers.get("Content-Length") or 0)
            if not size:
                return "unknown", 0
            result = ("ok" if size >= MIN_INK_BYTES else "thin"), size
    except urllib.error.HTTPError as e:
        result = ("dead" if e.code in DEAD_CODES else "unknown"), 0
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
            ConnectionError, OSError, ValueError):
        return "unknown", 0
    _checked[url] = result
    return result


# ============================================================
# payload
# ============================================================

# A caption box has a fixed height; a description does not. 6% of the
# pool has none at all and 37% runs to a full paragraph, so a layout that
# wants one predictable line needs one cut for it.
# A sentence ends on a long word or on a year -- "...on July 1, 1862."
# is a sentence end, while "no. 4." and "Wis." are not. Requiring four
# characters of one kind or the other in front of the stop is what tells
# them apart.
SENTENCE_SPLIT = re.compile(
    r"(?:(?<=[a-z]{4}[.!?])|(?<=\d{4}[.!?]))\s+(?=[\"'\[(A-Z])")


def short_description(text, limit=120):
    """
    One sentence that earns its place. Descriptions are a run of
    catalogue notes and the first is often the blandest ("Relief shown
    pictorially."), so take the longest that fits rather than the first
    -- length is a decent proxy for which note actually says something.
    """
    text = (text or "").strip()
    if not text or len(text) <= limit:
        return text
    sentences = [s.strip() for s in SENTENCE_SPLIT.split(text) if s.strip()]
    fitting = [s for s in sentences if len(s) <= limit]
    if fitting:
        # The opening sentence if it says anything -- it is the one the
        # cataloguer led with. Otherwise the fullest one that fits, since
        # a lead like "Relief shown pictorially." is filler and the
        # sentence after it is the one worth reading.
        if fitting[0] is sentences[0] and len(sentences[0]) >= 40:
            return sentences[0]
        return max(fitting, key=len)
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.-") + "..."


def cell_label(topic):
    """"Bird's-Eye Views" or "Bird's-Eye Views, 1870 - 1899"."""
    if CELL_SEP in topic:
        theme, era = topic.split(CELL_SEP, 1)
        return "{}, {}".format(TOPIC_LABELS.get(theme, theme),
                               TOPIC_LABELS.get(era, era))
    return TOPIC_LABELS.get(topic, topic.replace("-", " ").title())


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
        "day_index": day_index(day),
        "category": category,
        "category_label": cell_label(category),
        "heading": "MAP OF THE DAY",

        "title": entry["t"],
        "title_short": title_line(entry),
        "year": str(entry["y"]),
        "creator": creator,
        "place": place,
        "collection": entry.get("col", ""),
        "description": entry.get("d", ""),
        "description_short": short_description(entry.get("d", "")),
        "medium": entry.get("m", ""),
        "published": entry.get("pub", ""),
        "scale": entry.get("sc", ""),
        "subjects": entry.get("subj", []),
        # Separated with a dot, not a comma: a subject term can contain
        # a comma of its own ("Civil War, 1861-1865").
        "subjects_line": " \u00b7 ".join(entry.get("subj", [])),

        # Ready-made lines, for the common case where the layout wants one
        # string under the title rather than four fields to arrange.
        "byline": " - ".join(p for p in (creator, str(entry["y"])) if p),
        "subtitle": " - ".join(p for p in (place, entry.get("col", "")) if p),
        # Imprint and scale, the two details that are specific to a map
        # rather than to its subject. Either may be missing.
        "imprint": " - ".join(p for p in (entry.get("pub", ""),
                                          entry.get("sc", "")) if p),

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

    # 6. Every topic offered as a setting is deep enough that a reader
    #    does not see the same map twice inside a year.
    for topic in [s for s, _ in THEMES] + [s for s, _, _, _ in ERAS]:
        count = len(maps_for(entries, topic))
        if count < 200:
            failures.append("topic {} selects only {} maps"
                            .format(topic, count))

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
        print("ok: {} maps, {} topics, no repeats within a cycle of "
              "{} days".format(total,
                               len(THEMES) + len(ERAS), total))
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


def substantive(payload):
    """The payload minus the fields that move on their own. Recurses, so
    the combined file is compared by its maps rather than its clock."""
    out = {}
    for k, v in payload.items():
        if k in VOLATILE_FIELDS:
            continue
        if isinstance(v, dict):
            v = {kk: substantive(vv) if isinstance(vv, dict) else vv
                 for kk, vv in v.items()}
        out[k] = v
    return out


def write_json(path, payload):
    """Write the file, unless the only thing that changed is the clock."""
    try:
        with open(path) as fh:
            existing = json.load(fh)
        if substantive(existing) == substantive(payload):
            return False
    except (OSError, ValueError):
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
        fh.write("\n")
    return True


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

    themes = [slug for slug, _ in THEMES]
    eras = [slug for slug, _, _, _ in ERAS]

    # Every cell deep enough to offer. Which ones exist is data, not a
    # rule, so the markup reads the list rather than hardcoding it.
    cells = []
    for theme in themes:
        if theme == "all":
            continue
        for era in eras:
            size = len(maps_for(entries, cell_key(theme, era)))
            if size >= CELL_MIN:
                cells.append({"key": cell_key(theme, era), "theme": theme,
                              "era": era, "size": size})

    topics = themes + eras + [c["key"] for c in cells]
    picks, written = {}, []
    for topic in topics:
        subset = maps_for(entries, topic)
        if not subset:
            sys.stderr.write("  {} selects no maps, skipping\n".format(topic))
            continue
        entry, checked, size = pick(subset, topic, day,
                                    check=not args.no_check)
        payload = build_payload(entry, topic, day, pool, checked, size)
        payload["topic_size"] = len(subset)
        picks[topic] = payload
        if CELL_SEP not in topic:
            print("{:<18} {} ({}) [{}]".format(
                topic, payload["title_short"][:52], payload["year"], checked))

    if not args.dry_run:
        # map.json: one map, for a plugin that wants no settings at all.
        if write_json(DEFAULT_PATH, picks["all"]):
            written.append(os.path.relpath(DEFAULT_PATH, os.getcwd()))
        # today.json: every topic's map, so a plugin can offer any
        # combination of interests without a file per combination.
        combined = {
            "date": day.isoformat(),
            "day_index": day_index(day),
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pool_size": pool["count"],
            "themes": [{"key": t, "label": TOPIC_LABELS[t],
                        "size": picks[t]["topic_size"]}
                       for t in themes if t in picks],
            "eras": [{"key": t, "label": TOPIC_LABELS[t],
                      "size": picks[t]["topic_size"]}
                     for t in eras if t in picks],
            # The theme-and-era combinations that exist, and a flat list
            # of their keys so markup can test one with `contains`.
            "cells": [c for c in cells if c["key"] in picks],
            "cell_keys": [c["key"] for c in cells if c["key"] in picks],
            # TRMNL select options are plain strings, so a setting comes
            # back as the label a person saw ("City Plans"), not the key
            # this file uses. Ship the translation rather than making
            # every template hardcode it.
            "keys_by_label": {TOPIC_LABELS[t]: t for t in themes + eras
                              if t in picks},
            "picks": picks,
        }
        print("{} picks: {} themes, {} eras, {} cells".format(
            len(picks), len(themes), len(eras), len(cells)))
        if write_json(TOPICS_PATH, combined):
            written.append(os.path.relpath(TOPICS_PATH, os.getcwd()))

    print("wrote " + ", ".join(written) if written
          else "same maps as the last run, nothing rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
