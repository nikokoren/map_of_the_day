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
from concurrent.futures import ThreadPoolExecutor
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
# The size the markup asks for, and the one the daily job warms. It has
# to be a single fixed size, and daily.py has to have asked for it
# first: LOC renders a IIIF derivative on demand from a master that can
# be 11,000 pixels across, and the *first* request for a given size on a
# large map takes 12 to 18 seconds. Measured on the 1862 Little Falls
# view (11016x7176): !1872,1404 cold was 15.2s to first byte, and 0.43s
# once cached. TRMNL's renderer gives up long before 15 seconds, which
# is a blank panel with the caption still on it.
#
# So the markup must never compose a size of its own -- a device-derived
# box is a size nobody has warmed, every time the map changes.
WARM_BOX = (1872, 1404)
WARM_QUALITY = "default"
WARM_WORKERS = 6
WARM_TIMEOUT = 75

MAX_SKIPS = 4

# How far either side of a day a stand-in has to stay clear of that
# day's scheduled map, so covering a dead image never reads as a repeat.
# The feed itself is three days wide; this is wider than the window a
# reader can compare.
NEAR_DAYS = 3

DEAD_CODES = (403, 404, 410, 451)
CHECK_TIMEOUT = 12

# How much ink the map has to carry, as bytes per thousand pixels of the
# probe render. Cheaper and more honest than any metadata field, because
# it measures the picture itself: a hand-drawn plat of four blocks comes
# back around 70, an engraved city view or railroad map at 200 or more.
#
# Per thousand pixels, not in total, and that distinction is the whole
# point. PROBE_BOX is a box, and IIIF fits the sheet inside it, so how
# many pixels come back depends on the sheet's shape: a tall map renders
# about 0.17 megapixels, a wide one 0.38. A flat byte count therefore
# asked a tall map for twice the ink density of a wide one to clear the
# same bar, and rejected 36% of tall maps against 3% of wide ones -- a
# filter on shape wearing a filter on content's clothes. Measured over
# 400 maps, this rejects 2-3% at every shape.
MIN_INK_DENSITY = 90

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
    ("world-and-hemispheres", "World & Hemispheres"),
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

    The stand-ins are drawn from half a cycle away, not from the days
    that follow. `ordered` *is* the schedule, so ordered[position + 1] is
    not a spare map, it is tomorrow's map: stepping into it to dodge a
    dead image hands the reader the same map two days running, and the
    repair for a missing image becomes the thing they actually notice.
    (It is what put the same chart of the coast of Maine on screen on
    both 2026-09-16 and 2026-09-17.)

    Half a cycle is the furthest point from the day being covered, in
    either direction -- twelve days for the smallest topic the plugin
    offers, years for the pool as a whole. A stand-in still comes round
    again on its own day, once, that far away.
    """
    index = day_index(day)
    total = len(entries)
    cycle, position = divmod(index, total)
    ordered = order_for(entries, category, cycle)
    picks = [ordered[position]]
    gap = max(1, total // 2)
    for offset in range(MAX_SKIPS):
        stand_in = (position + gap + offset) % total
        # Only reachable on a pool far smaller than any topic offered,
        # where half a cycle wraps back onto the day itself.
        if stand_in != position:
            picks.append(ordered[stand_in])
    return picks


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
        # The one the markup uses, and the one warm() warms. Same box,
        # same quality, same string -- a mismatch in any of the three
        # and the device is back to waiting for a cold render.
        "image": iiif(entry["s"], WARM_BOX, WARM_QUALITY),
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


def probe_kilopixels(entry):
    """
    How many thousand pixels the probe render comes back as. The master
    dimensions are in the pool, so this costs nothing and needs no
    download: IIIF fits the sheet inside PROBE_BOX without cropping or
    stretching it, which is the same arithmetic.

    None when the pool does not record the size, which leaves the ink
    check with nothing to normalise against and so no opinion.
    """
    width, height = entry.get("w") or 0, entry.get("h") or 0
    if not width or not height:
        return None
    scale = min(PROBE_BOX[0] / width, PROBE_BOX[1] / height, 1.0)
    return (width * scale) * (height * scale) / 1000


def image_state(url, kilopixels=None):
    """
    ('ok' | 'thin' | 'dead' | 'unknown', bytes). Only 'dead' and 'thin'
    move the pick; a timeout or a 500 leaves the day's map exactly where
    it was, which is the difference between a bad minute at LOC and a
    different map. A HEAD is enough -- the image service reports the
    rendered size without sending the picture.

    Without kilopixels there is nothing to measure density against, so
    the map is taken as it is rather than judged on its byte count.
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
            if kilopixels:
                thin = size / kilopixels < MIN_INK_DENSITY
            else:
                thin = False
            result = ("thin" if thin else "ok"), size
    except urllib.error.HTTPError as e:
        result = ("dead" if e.code in DEAD_CODES else "unknown"), 0
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
            ConnectionError, OSError, ValueError):
        return "unknown", 0
    _checked[url] = result
    return result


def warm(services):
    """
    Ask LOC for every derivative the recipe is about to point devices
    at, so the first device to ask gets a cached file instead of a
    render. A HEAD is enough -- the service still has to produce the
    image to report its length, and a HEAD on a cold size measured 3.0s
    against 0.41s for the GET that followed it.

    Failures are not fatal and not even reported per map: a derivative
    that would not warm is a derivative the device will wait for, which
    is the situation this is improving on, not one it has to guarantee.
    """
    urls = sorted({iiif(service, WARM_BOX, WARM_QUALITY)
                   for service in services})
    if not urls:
        return 0

    def touch(url):
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=WARM_TIMEOUT) as resp:
                return resp.status == 200
        except Exception:
            return False

    started = time.monotonic()
    total, warmed = len(urls), 0
    # Two passes. A render that ran past the timeout on the first pass
    # has usually finished by the second, and the file is then sitting in
    # the cache waiting to be acknowledged rather than made again.
    for attempt in (1, 2):
        with ThreadPoolExecutor(max_workers=WARM_WORKERS) as pool:
            results = list(pool.map(touch, urls))
        warmed += sum(1 for ok in results if ok)
        urls = [url for url, ok in zip(urls, results) if not ok]
        if not urls:
            break
    print("warmed {}/{} images in {:.0f}s{}".format(
        warmed, total, time.monotonic() - started,
        ", {} still cold".format(len(urls)) if urls else ""))
    return warmed


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
ROLE_SUFFIX = re.compile(
    r"[\s,]*\b(?:Creator|Author|Artist|Cartographer|Engraver|Editor|"
    r"Publisher|Contributor|Compiler|Surveyor|Lithographer|Draftsman|"
    r"Printer|Illustrator|Translator|Associated Name|Former Owner|"
    r"Donor|Engraver|Owner|Etcher|Collaborator|Annotator|Recipient|"
    r"Signer|Bookseller|Dedicatee|Patron|Binder|Papermaker|Draughtsman|"
    r"Attributed Name|cartr?ographer|engraver)"
    r"\.?\s*$", re.I)

SENTENCE_SPLIT = re.compile(
    r"(?:(?<=[a-z]{4}[.!?])|(?<=\d{4}[.!?]))\s+(?=[\"'\[(A-Z])")


# ============================================================
# byline
# ============================================================
#
# The caption was reading:
#
#   Published by Porter, Seward, 1784-1838 in Bath, Me.? : Seward Porter, 1837
#
# which is three faults in one line. The creator is a catalogue heading,
# surname first with life dates attached -- 73% of the pool is inverted
# and 33% carries dates. The published string is a MARC 260 imprint,
# "Place : Publisher, Date", so the "in" was followed by a publisher and
# a colon rather than a place. And in 932 cases that publisher is the
# creator, printed twice in one breath.
#
# What a reader wants is who made it, where it was published and when.
# For a postcard the place of printing is a trap -- German lithographers
# printed views of everywhere -- but a map is different: Paris in 1790
# against London in 1776 is the provenance, and what the map depicts is
# in the title already.

# "1784-1838", "1625?-", "approximately 1600-", "active 1750-1776" and
# "17th century" -- the question mark sits on either side of the dash.
LIFE_DATES = re.compile(
    r"[\s,]*\(?(?:active\s+|fl\.?\s*|b\.?\s*|d\.?\s*|ca\.?\s*|circa\s+|approximately\s+)?"
    r"(?:[-\u2013]\s*(?:\d{3,4}\??|cir\w*)"
    r"|\d{3,4}\??(?:\s*or\s*\d+)?\s*(?:[-\u2013]\s*(?:\d{0,4}\??|cir\w*))?"
    r"|\d{1,2}(?:st|nd|rd|th)\s+century)\)?\.?\s*$", re.I)
# A rank or role trailing a second comma. Jr and Sr are kept: they belong
# to the name, where "Sir" and "surveyor general" belong to the record.
HONORIFIC = re.compile(
    r",\s*(?:Sir|Dame|Lord|Lady|Mrs|Mr|Dr|Rev|Hon|Capt|Captain|Lt|Lieut|"
    r"Lieutenant|Col|Colonel|Gen|General|Maj|Major|Sgt|surveyor general|"
    r"surveyor|engineer|cartographer|delineator)\.?\s*$", re.I)
NAME_SUFFIX = re.compile(r",\s*(Jr|Sr|II|III)\.?\s*$", re.I)
PARENTHETICAL = re.compile(r"\s*\([^)]*\)")
PARTICLE = re.compile(
    r"^(?:d'|de|del|della|van|von|der|den|le|la|du|des|di|da|af|zu|ten|ter)$", re.I)
# A firm is not a person and must never be turned around: "G.W. & C.B.
# Colton & Co" is the name of the house, not Colton's surname.
FIRM = re.compile(r"&|\b(?:Co|Inc|Ltd|Bros|Sons|Company|Firm)\b\.?", re.I)
NAME_PART = re.compile(r"^[^\W\d_][\w'.\u0301\u0306\ufe20\ufe21-]*\.?$", re.UNICODE)


def person(name):
    """'Porter, Seward, 1784-1838' -> 'Seward Porter'. Bodies left alone."""
    n = (name or "").strip().strip("[]").strip()
    if not n:
        return ""
    for _ in range(3):                    # role and dates stack, either order
        n = ROLE_SUFFIX.sub("", n)
        n = LIFE_DATES.sub("", n)
    n = PARENTHETICAL.sub("", n).strip().strip(" ,;")
    n = re.sub(r"[\s,]*[-\u2013]\s*$", "", n).strip(" ,;")   # a dash with no date behind it
    for _ in range(2):
        n = HONORIFIC.sub("", n).strip().strip(" ,;")
    suffix = ""
    found = NAME_SUFFIX.search(n)
    if found:
        suffix, n = ", " + found.group(1) + ".", n[:found.start()].strip()
    if n.count(",") != 1 or FIRM.search(n):
        return n + suffix                 # a body, a firm, or too tangled
    surname, rest = (part.strip() for part in n.split(","))
    parts = rest.split()
    if not parts or len(parts) > 5 or not all(
            NAME_PART.match(part) or PARTICLE.match(part) for part in parts):
        return n + suffix
    lead = []
    while parts and PARTICLE.match(parts[-1].rstrip(".")):
        lead.insert(0, parts.pop())
    given, particle = " ".join(parts), " ".join(lead)
    joined = (particle + surname) if particle.endswith("'") else \
        " ".join(part for part in (particle, surname) if part)
    return (" ".join(part for part in (given, joined) if part) + suffix).strip()


PLACE_UNKNOWN = re.compile(
    r"^\[?(?:s\.?\s*l\.?|s\.?\s*n\.?|n\.?\s*p\.?|"
    r"(?:place of publication|publisher) not identified)[\]\s.,]*"
    r"(?:s\.?\s*n\.?|s\.?\s*l\.?)?[\]\s.,]*$", re.I)
# A qualifier belonging to the place: "Bath, Me.", "Washington, D.C."
PLACE_QUALIFIER = re.compile(r"^(?:[A-Z]{1,2}\.?[A-Z]?\.?|[A-Z][a-z]{1,10}\.?)$")
# Anything from the printing trade means the publisher has crept in.
TRADE = re.compile(r"&|\b(?:lith|litho|lithograph\w*|engr\w*|print\w*|pub\w*|"
                   r"drawn|sc|del|Co|Inc|Ltd|Bros|Sons)\b\.?", re.I)


def published_in(imprint):
    """
    The place of publication, when the imprint is structured enough to say.

    Only half these imprints keep to the shape MARC 260 describes. A
    colon separates place from publisher and can be trusted; a bare comma
    cannot, because "New York, Hughes & Bailey, c1916" puts the firm in
    the middle. So: everything before the colon, or before the first
    comma, plus one short qualifier -- and nothing at all when the string
    is free text. 62% of the pool yields a place that way, and the rest
    says nothing rather than saying "S.l." to somebody's wall.
    """
    text = (imprint or "").strip()
    if not text:
        return ""
    text = re.split(r"\s*:\s*", text, 1)[0]
    parts = [part.strip() for part in text.split(",")]
    place = parts[0]
    if len(parts) > 1 and PLACE_QUALIFIER.match(parts[1].rstrip("?")):
        place = place + ", " + parts[1]
    place = re.sub(r"\s*\?", "", place).strip().strip("[]").strip(" ,;:")
    if not place or PLACE_UNKNOWN.match(place) or TRADE.search(place):
        return ""
    if re.search(r"\d", place) or len(place.split()) > 4:
        return ""
    return place


def byline(entry):
    """
    Who made it, where it came out, and when -- as one readable clause.

    The caption sits beside the title rather than under it, so this has
    to read on from the title in smaller type: "Chart of the coast of
    Maine" then "by Seward Porter in Bath, Me., 1837". Every branch is
    prose, because a branch that starts mid-sentence reads as a bug.
    """
    who = person(entry.get("c"))
    # A body's trailing stop reads badly before our comma -- "Railway
    # Company., 1898" -- but an initial and a generational suffix keep
    # theirs: "A. J." and "Lucas, Jr." are not sentences ending.
    last = who.rsplit(" ", 1)[-1]
    if who.endswith(".") and len(last) > 2 and not NAME_SUFFIX.search(who):
        who = who[:-1]
    where = published_in(entry.get("pub"))
    year = str(entry.get("y") or "")
    if who and where:
        return "by {} in {}, {}".format(who, where, year).rstrip(", ")
    if who:
        return "by {}, {}".format(who, year).rstrip(", ")
    if where:
        return "Published in {}, {}".format(where, year).rstrip(", ")
    return year


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


def label_aliases(topics, picks):
    """
    Every string a topic might arrive as, mapped to its key. Covers the
    label itself, the lowercase-underscored form TRMNL derives from it,
    that form with the apostrophe or ampersand normalised, and the key.
    Cheap insurance: a settings panel and a feed disagreeing about a
    string is invisible until every selection quietly means "all maps".
    """
    out = {}
    for topic in topics:
        if topic not in picks:
            continue
        label = TOPIC_LABELS[topic]
        snake = label.lower().replace(" ", "_")
        plain = snake.replace("'", "").replace("&", "and")
        for alias in (label, label.lower(), snake, plain,
                      snake.replace("'", ""),
                      snake.replace("&", "and"),
                      plain.replace("-", "_"),
                      re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", plain)),
                      topic):
            out[alias] = topic
    return out


# TRMNL refuses a polling response over 100KB, so the combined feed
# carries only what markup cannot derive. Every image URL is a suffix on
# image_base, the full description is a superset of description_short,
# and the credit lines are the same on every map -- so those ride once at
# the top level, or not at all.
# The feed carries three days, not one, and this is why.
#
# The pick used to be chosen here against the UTC date and baked into
# the file, so the map changed at the same instant worldwide. In Berlin
# that is 02:05, which reads as a new day. In Los Angeles it is 17:05
# the *previous* afternoon, and in Auckland it lands at midday and
# swaps the map while somebody is looking at it.
#
# The device knows better: trmnl.system.timestamp_utc plus
# trmnl.user.utc_offset is the viewer's own local time, so the markup
# works out its own local day and asks for that day's map.
#
# Three days, because offsets run -12 to +14: the 24 hours one file is
# live span about 50 hours of local time, which always crosses two or
# three midnights, and it comes to three for any publish hour.
DAY_SPAN = (-1, 0, 1)

# A pick is a list, not an object. At 55 cells across 3 days, field
# names alone would cost about 20KB of the 95KB TRMNL allows. The order
# is the contract with the markup, and selection.liquid unpacks it into
# named variables so the layout stays readable.
PICK_FIELDS = (
    "image", "title_short", "year", "byline",
    "description_short", "place", "category_label", "item_id",
)

# Refuse to publish a feed TRMNL will reject. Failing loudly leaves
# yesterday's good file on the screen; shipping an oversized one takes
# the plugin into a degraded state and stops it refreshing.
MAX_FEED_BYTES = 95_000


def slim(payload):
    return [payload.get(k, "") for k in PICK_FIELDS]


def published_days(feed):
    """{day: {topic: pick}} out of a feed file, or nothing usable."""
    days = feed.get("days")
    if not isinstance(days, dict):
        return {}
    # A feed written against a different PICK_FIELDS cannot be carried
    # forward pick by pick, because the markup unpacks by position.
    if list(feed.get("pick_fields") or []) != list(PICK_FIELDS):
        return {}
    return {day: picks for day, picks in days.items()
            if isinstance(picks, dict)}


def load_published(path=TOPICS_PATH):
    """The live feed, or nothing if there isn't one to read."""
    try:
        with open(path) as fh:
            return published_days(json.load(fh))
    except (OSError, ValueError):
        return {}


def image_alive(url):
    """
    False only when the image is definitively gone. A timeout or a 500 is
    not evidence of anything, and must not unseat a map that is already
    on screens.
    """
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT) as resp:
            return resp.status < 400
    except urllib.error.HTTPError as e:
        return e.code not in DEAD_CODES
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
            ConnectionError, OSError, ValueError):
        return True


def still_stands(standing, probe):
    """
    Whether a pick that has already gone out can stay. Shape first --
    anything the markup could not unpack is not a pick -- and then, on
    the day it matters, whether the image is still there.

    Thinness is deliberately not rechecked. Whether the map carried
    enough ink was settled when it was chosen; asking again would let a
    changed threshold move a day somebody is already looking at, which
    is the thing this is here to prevent.
    """
    if not isinstance(standing, list) or len(standing) != len(PICK_FIELDS):
        return False
    if not all(isinstance(field, str) for field in standing):
        return False
    if not standing[0]:
        return False
    if not probe or budget_left() <= 0:
        return True
    return image_alive(standing[0])


def cell_label(topic):
    """"Bird's-Eye Views" or "Bird's-Eye Views, 1870 - 1899"."""
    if CELL_SEP in topic:
        theme, era = topic.split(CELL_SEP, 1)
        return "{}, {}".format(TOPIC_LABELS.get(theme, theme),
                               TOPIC_LABELS.get(era, era))
    return TOPIC_LABELS.get(topic, topic.replace("-", " ").title())


# How long a headline may run before it is cut. Titles here are
# catalogue titles: 45% are over 64 characters and the long ones are
# often a whole sentence of description, so the cut has to land
# somewhere that reads as a phrase rather than mid-clause.
#
# 120 rather than something smaller because the titles that get cut are
# not marginal -- their median full length is 134 characters. Measured
# over the pool, 88 truncates 10% and 120 truncates 4%, while the median
# caption grows only from 51 to 55 characters: raising the limit does
# not lengthen most captions, it just stops chopping the long ones. The
# longest caption anyone sees is 122 characters.
TITLE_LIMIT = 120
SUBTITLE_MARKERS = (" : ", " ; ", " -- ", " \u2014 ")


def balance_brackets(text):
    """A cut inside a devised title leaves "[North America from the" --
    close it rather than leave the bracket hanging."""
    if text.count("[") > text.count("]"):
        text += "]"
    if text.count("(") > text.count(")"):
        text += ")"
    return text


TRANSLATIONS_PATH = os.path.join(HERE, "translations.json")
_english = {}


def load_translations():
    global _english
    try:
        with open(TRANSLATIONS_PATH) as fh:
            _english = json.load(fh).get("titles") or {}
    except (OSError, ValueError):
        _english = {}
    return _english


def title_line(entry):
    """
    The title, trimmed to something that fits a headline. The full title
    stays available as `title`.

    Cut at a subtitle marker where there is one, otherwise at the last
    clause boundary that fits, and only as a last resort mid-phrase with
    an ellipsis -- which is 10% of the pool rather than the 37% a plain
    character cut produced.
    """
    rendered = _english.get(entry["t"]) or {}
    # English replaces the original rather than joining it: the panel has
    # room for one caption, and a reader who cannot read Latin is not
    # helped by being shown the Latin as well. The pool keeps both.
    title = rendered.get("en") or entry["t"]
    if len(title) <= TITLE_LIMIT:
        return title

    for marker in SUBTITLE_MARKERS:
        if marker in title:
            head = title.split(marker)[0].strip(" ,:;-")
            if 20 <= len(head) <= TITLE_LIMIT:
                return balance_brackets(head)

    window = title[:TITLE_LIMIT + 1]
    cut = max(window.rfind(", "), window.rfind("; "), window.rfind(" -- "))
    if cut >= 25:
        return balance_brackets(window[:cut].strip(" ,;:-"))

    trimmed = window.rsplit(" ", 1)[0].rstrip(" ,;:.-")
    return balance_brackets(trimmed) + "\u2026"


def build_payload(entry, category, day, pool, checked, ink_bytes=0):
    aspect = round(entry["w"] / float(entry["h"]), 3)
    urls = image_urls(entry)
    # "Braun, Georg, 1540 or 1541-1622 Creator." -- the catalogue's role
    # term, which belongs in a record and not in a byline. 283 of 4,971
    # maps carry one. Stripped here rather than in harvest.py so the
    # committed pool is fixed without waiting for a re-crawl.
    creator = ROLE_SUFFIX.sub("", entry.get("c") or "").strip(" ,;")
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
        "byline": byline(entry),
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
        state, size = image_state(image_urls(entry)["probe"],
                                  probe_kilopixels(entry))
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

    # 1b. The byline, at the strings that taught it each rule. A
    #     catalogue heading is not a byline: surname-first with life
    #     dates, a MARC imprint with the publisher in the middle, and a
    #     cataloguer's "S.l." for a place nobody recorded.
    for want, creator, imprint, year in (
            ("by Seward Porter in Bath, Me., 1837",
             "Porter, Seward, 1784-1838", "Bath, Me.? : Seward Porter, 1837", 1837),
            ("by Jean Baptiste Bourguignon d'Anville in Paris, 1790",
             "Anville, Jean Baptiste Bourguignon d', 1697-1782",
             "Paris : Dezauche, 1790?", 1790),
            # a body and a firm are never turned around
            # s.n. means the publisher is unrecorded, not the place --
            # Washington is known and belongs in the line
            ("by United States. Corps of Topographical Engineers in "
             "Washington, D.C., 1862",
             "United States. Corps of Topographical Engineers",
             "Washington, D.C.? : s.n., 1862", 1862),
            ("by G.W. & C.B. Colton & Co in New York, 1872",
             "G.W. & C.B. Colton & Co", "New York, 1872", 1872),
            # the firm in the middle of an imprint is not the place
            ("by T. M. Fowler in New York, 1916",
             "Fowler, T. M. (Thaddeus Mortimer), 1842-1922",
             "New York, Hughes & Bailey, c1916", 1916),
            # a rank belongs to the record, a generational suffix to the name
            ("by J. H. Baker in Saint Paul, 1874",
             "Baker, J. H., surveyor general", "Saint Paul : s.n., 1874", 1874),
            ("by Fielding Lucas, Jr. in Baltimore, 1823",
             "Lucas, Fielding, Jr.", "Baltimore : F. Lucas, 1823", 1823),
            # nothing recorded beats printing the abbreviation for nothing
            ("by N. Michler, 1867",
             "Michler, N. (Nathaniel), 1827-1881", "S.l. : s.n., 1867", 1867),
            ("1863", "", "", 1863),
    ):
        got = byline({"c": creator, "pub": imprint, "y": year})
        if got != want:
            failures.append("byline: {!r} not {!r}".format(got, want))

    # 1c. And no catalogue shorthand reaches a screen, anywhere in the pool.
    junk = re.compile(r"\bS\.l\.|\bs\.n\.|18--|\?|not identified", re.I)
    leaked = [e["id"] for e in entries if junk.search(byline(e))]
    if leaked:
        failures.append("{} bylines still carry catalogue shorthand, e.g. {}"
                        .format(len(leaked), byline(
                            [e for e in entries if e["id"] == leaked[0]][0])))

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

    # 5b. And a stand-in is never a nearby day's map. Check 5 only ever
    #     looked at candidates_for(...)[0], the scheduled pick, so it saw
    #     a clean schedule while the skip path quietly served tomorrow's
    #     map to cover today's dead image -- a repeat the reader sees and
    #     the test did not. The whole candidate list has to be clear of
    #     the days around it, not just the first entry.
    #     The smallest topic rides along, because a topic is a much
    #     shorter cycle than the pool and is the only place the wrap
    #     guard in candidates_for can fire. It is also where the reader
    #     met this bug: Nautical Charts, not the whole collection.
    topics = [s for s, _ in THEMES] + [s for s, _, _, _ in ERAS]
    smallest = min(topics, key=lambda s: len(maps_for(entries, s)))
    for topic in ("all", smallest):
        sub = maps_for(entries, topic)
        span = len(sub)
        for offset in (0, 1, 2, span // 3):
            d = day + timedelta(days=offset)
            if day_index(d) // span != day_index(day) // span:
                continue
            stand_ins = {e["id"] for e in candidates_for(sub, topic, d)[1:]}
            if not stand_ins:
                failures.append("{}: no stand-in at all on {}"
                                .format(topic, d.isoformat()))
                break
            for near in range(-NEAR_DAYS, NEAR_DAYS + 1):
                n = d + timedelta(days=near)
                if day_index(n) // span != day_index(d) // span:
                    continue
                clash = candidates_for(sub, topic, n)[0]["id"]
                if clash in stand_ins:
                    failures.append(
                        "{}: a stand-in on {} is the map scheduled for {}"
                        .format(topic, d.isoformat(), n.isoformat()))
                    break

    # 5c. The carry-forward contract. A published day is only safe to
    #     copy across if the row still means what the markup will unpack,
    #     so the shape rules are what keep a bad feed from propagating.
    good = slim(build_payload(entries[0], "all", day, {"count": total}, "ok"))
    for ok_expected, row, why in (
            (True,  good,                        "a well-formed row stands"),
            (False, good[:-1],                   "a short row is not a pick"),
            (False, good + [""],                 "a long row is not a pick"),
            (False, [""] + good[1:],             "a row with no image is not a pick"),
            (False, dict(enumerate(good)),       "a dict is not a row"),
            (False, [None] + good[1:],           "a non-string field is not a pick"),
    ):
        if still_stands(row, probe=False) is not ok_expected:
            failures.append("carry-forward: " + why)
    # And a feed written against different fields cannot be carried at
    # all, because the markup unpacks a row by position.
    live = {"pick_fields": list(PICK_FIELDS), "days": {"1": {"all": good}}}
    if not published_days(live):
        failures.append("carry-forward: a matching feed was not read back")
    for bad in ({"pick_fields": list(PICK_FIELDS)[:-1], "days": {"1": {}}},
                {"days": {"1": {}}},
                {"pick_fields": list(PICK_FIELDS), "days": []}):
        if published_days(bad):
            failures.append("carry-forward: a mismatched feed was carried")

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
    parser.add_argument("--recompute", action="store_true",
                        help="choose every day afresh, ignoring what is "
                             "already published")
    args = parser.parse_args()

    load_translations()
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

    # What the last run put out. A day it already published is a day
    # somebody is already looking at: offsets run to +14, so a viewer can
    # be on tomorrow's map eighteen hours before the next run replaces
    # the file, and a viewer at -12 is still on yesterday's when it
    # lands. Choosing those days again would move the map under them --
    # once at their own midnight, which is the point of the three-day
    # feed, and again when the new file arrives, which is not.
    #
    # And it would, because the schedule turns on the pool: divmod by its
    # size and a hash ordering over its membership, so anything that
    # moves the pool moves every topic's calendar with it. The monthly
    # harvest does that wholesale, and the image probe does it piecemeal,
    # since only the middle day is probed and a stand-in there disagrees
    # with the unprobed copy published yesterday.
    #
    # So a published pick stands. The one thing that unseats it is its
    # image having gone, which is worse than the change.
    by_id = {e["id"]: e for e in entries}
    published = {} if args.recompute else load_published()
    item_at = PICK_FIELDS.index("item_id")
    title_at = PICK_FIELDS.index("title_short")
    year_at = PICK_FIELDS.index("year")

    # Every topic, for every day a device might be on. A map that is
    # tomorrow's here is today's for somebody fourteen hours ahead.
    days, written, chosen_services, sizes = {}, [], [], {}
    picks = {}
    carried = unresolved = 0
    for shift in DAY_SPAN:
        that_day = day + timedelta(days=shift)
        standing_day = published.get(str(day_index(that_day))) or {}
        day_picks = {}
        for topic in topics:
            subset = maps_for(entries, topic)
            if not subset:
                if shift == 0:
                    sys.stderr.write(
                        "  {} selects no maps, skipping\n".format(topic))
                continue
            # Only probe images for the middle day. The other two are
            # the same maps a day either side of their own turn, and get
            # probed when it comes.
            probe = not args.no_check and shift == 0
            sizes[topic] = len(subset)

            standing = standing_day.get(topic)
            if still_stands(standing, probe):
                day_picks[topic] = standing
                carried += 1
                if shift != 0:
                    continue
                # map.json and the log want the map, not just the row the
                # device reads, so resolve it back through the item id
                # the row carries.
                held = by_id.get(standing[item_at])
                if held is not None:
                    chosen_services.append(held["s"])
                    picks[topic] = build_payload(held, topic, that_day,
                                                 pool, "carried")
                    picks[topic]["topic_size"] = len(subset)
                    # Not re-probed, so there is no reading to report.
                    # Absent says that; 0 would claim a blank sheet.
                    picks[topic].pop("ink_bytes", None)
                    if CELL_SEP not in topic:
                        print("{:<18} {} ({}) [carried]".format(
                            topic, standing[title_at][:52], standing[year_at]))
                    continue
                # The map left the pool since it was published. The row
                # still stands for the device -- that is the promise --
                # but map.json and the options list need a map that is
                # still here, so those fall through to a fresh pick.
                unresolved += 1
                sys.stderr.write(
                    "  {} holds {}, no longer in the pool; the feed keeps "
                    "it, map.json does not\n".format(topic, standing[item_at]))

            entry, checked, size = pick(subset, topic, that_day, check=probe)
            payload = build_payload(entry, topic, that_day, pool, checked, size)
            payload["topic_size"] = len(subset)
            if topic not in day_picks:
                day_picks[topic] = payload
            chosen_services.append(entry["s"])
            if shift == 0:
                picks[topic] = payload
                if CELL_SEP not in topic:
                    print("{:<18} {} ({}) [{}]".format(
                        topic, payload["title_short"][:52],
                        payload["year"], checked))
        days[str(day_index(that_day))] = day_picks

    if not args.dry_run:
        # Warm every derivative before publishing the file that points
        # at it, so no device is ever the one that triggers the render.
        if not args.no_check:
            warm(chosen_services)
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
            # Named *_options, not themes/eras: a plugin's own settings
            # land in the same template context as the feed, and a
            # setting keyed "themes" would collide with a feed key of
            # the same name -- silently, with the feed winning.
            "default_day": str(day_index(day)),
            "pick_fields": list(PICK_FIELDS),
            "theme_options": [{"key": t, "label": TOPIC_LABELS[t],
                               "size": sizes[t]}
                              for t in themes if t in picks],
            "era_options": [{"key": t, "label": TOPIC_LABELS[t],
                             "size": sizes[t]}
                            for t in eras if t in picks],
            # The theme-and-era combinations that exist, and a flat list
            # of their keys so markup can test one with `contains`.
            "cells": [c for c in cells if c["key"] in picks],
            "cell_keys": [c["key"] for c in cells if c["key"] in picks],
            # A TRMNL select stores a value derived from the option a
            # person picked, not the option itself: "The 1700s" arrives
            # as "the_1700s" and "1800 - 1849" as "1800_-_1849". Ship
            # every spelling a setting might arrive as, so markup can
            # look one up without transforming anything.
            "keys_by_label": label_aliases(themes + eras, picks),
            # Constant on every map, so said once rather than 48 times.
            "heading": "MAP OF THE DAY",
            "source": CREDIT,
            "credit": "Library of Congress",
            "rights": RIGHTS,
            "item_url_prefix": "https://www.loc.gov/item/",
            # A fresh pick is a payload and gets slimmed to a row; a
            # carried one was read back as a row already.
            "days": {d: {k: (v if isinstance(v, list) else slim(v))
                         for k, v in p.items()}
                     for d, p in days.items()},
        }
        print("{} days x {} picks: {} themes, {} eras, {} cells, "
              "{} carried forward".format(
                  len(days), len(picks), len(themes), len(eras), len(cells),
                  carried))
        size = len(json.dumps(combined, separators=(",", ":"),
                              sort_keys=True).encode("utf-8"))
        print("today.json {:.1f}KB ({} picks)".format(size / 1024.0,
                                                      len(picks)))
        if size > MAX_FEED_BYTES:
            sys.stderr.write(
                "today.json is {}B, over the {}B TRMNL accepts; refusing "
                "to publish it. Trim PICK_FIELDS or raise CELL_MIN.\n"
                .format(size, MAX_FEED_BYTES))
            return 1
        if write_json(TOPICS_PATH, combined):
            written.append(os.path.relpath(TOPICS_PATH, os.getcwd()))

    print("wrote " + ", ".join(written) if written
          else "same maps as the last run, nothing rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
