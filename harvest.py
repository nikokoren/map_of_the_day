#!/usr/bin/env python3
"""
Builds map_of_the_day/pool.json, the candidate pool for the plugin.

Walks a handful of Library of Congress map collections, throws out
everything that would look bad (or be legally awkward) on a 1-bit e-ink
screen, and writes what survives as a compact pool. Nothing here runs on
the day the map is shown: daily.py picks from this file offline, so a bad
day at loc.gov can never blank the screen.

Run it monthly, or by hand:
    python3 map_of_the_day/harvest.py            # full harvest, writes pool
    python3 map_of_the_day/harvest.py --pages 2  # quick smoke test
    python3 map_of_the_day/harvest.py --dry-run  # print stats, write nothing

The LOC search API hands us everything we need in the results array --
title, date, rights flag, and the IIIF image URLs with their pixel
dimensions in the URL fragment. That means one request per 100 records and
no per-item follow-up, which keeps a full harvest around 150 requests.
"""

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

# ============================================================
# config
# ============================================================

HERE = os.path.dirname(os.path.abspath(__file__))
POOL_PATH = os.path.join(HERE, "pool.json")
QUALITY_PATH = os.path.join(HERE, "quality.json")

UA = "mission-control-trmnl/1.0 (github.com/nikokoren/mission_control)"

# One request per this many seconds. LOC publishes crawl limits per
# endpoint; this is well under the slowest of them and a monthly job has
# no reason to be in a hurry.
REQUEST_DELAY = 2.0
TIMEOUT = 60
RETRIES = 4

# The collections we harvest, and the category each one feeds. Categories
# become the user-selectable setting in TRMNL, so a category is only worth
# having if it has enough maps behind it to stay fresh for years.
#
# Slug is the loc.gov collection slug: loc.gov/collections/<slug>/.
# Label is what we show as the collection line on screen. The fourth
# item is extra query string, used where a collection holds more than
# maps -- World Digital Library is 21,000 items of which 1,827 are maps,
# and paging the rest to throw them away would be rude as well as slow.
SOURCES = [
    ("panoramas",   "panoramic-maps",                 "Panoramic Maps", ""),
    ("railways",    "railroad-maps-1828-to-1900",     "Railroad Maps, 1828-1900", ""),
    ("cities",      "cities-and-towns",               "Cities and Towns", ""),
    ("military",    "civil-war-maps",                 "Civil War Maps", ""),
    ("military",    "american-revolutionary-war-maps", "American Revolutionary War Maps", ""),
    ("military",    "military-battles-and-campaigns", "Military Battles and Campaigns", ""),
    ("exploration", "discovery-and-exploration",      "Discovery and Exploration", ""),
    ("nature",      "national-parks-maps",            "National Parks Maps", ""),
    # Charts of the North American coast, engraved for the Royal Navy in
    # the 1770s. Tiny, and among the finest line work in the pool.
    ("nautical",    "the-atlantic-neptune-collection", "The Atlantic Neptune",
     "&fa=original-format:map"),
    # Russia and Siberia, and the American west that faced it.
    ("frontiers",   "meeting-of-frontiers",           "Meeting of Frontiers",
     "&fa=original-format:map"),
    # The one collection here that is mostly not American.
    ("world",       "world-digital-library",          "World Digital Library",
     "&fa=original-format:map"),
]

MAX_PAGES = 25          # per collection, at 100 records a page
PER_PAGE = 100

# --- what gets thrown out ---

# Published this year or earlier. The Geography and Map Division says its
# digitized collections are free to use unless an item carries a rights
# advisory, and we drop flagged items anyway -- but a hard pre-1930 cutoff
# means no individual item ever needs a rights judgement call. Federal
# works after 1929 are public domain too; they are simply not worth the
# argument when the pool is already thousands of maps deep.
MAX_YEAR = 1929
MIN_YEAR = 1400

# A map smaller than this was scanned badly or is a tiny inset. The
# larger panel is 1404px on its short side, so a scan under about that
# is being upscaled before it is even dithered.
MIN_SHORT_SIDE = 1400
MIN_PIXELS = 2_500_000

# Aspect (width / height). The panels are landscape: 1.67 on the OG
# (800x480) and 1.33 on the larger one (1040x780). Very tall maps end up
# as a stripe down the middle and very wide ones as a band across it;
# both still read, extremes do not.
MIN_ASPECT = 0.55
MAX_ASPECT = 3.60

# The band the panels span. A map inside it fills whichever screen it
# lands on; outside it, the cost grows with the distance.
ASPECT_BAND = (1.33, 1.67)

# Titles that mean "one sheet of a set", "a photocopy", or "a page of an
# atlas" -- all of which look like a fragment on screen with no context.
TITLE_REJECT = [
    "sanborn", "insurance maps", "index to", "index map", "key map",
    "sheet no", "verso", "blank", "title page", "photocopy", "facsimile",
    "questionnaire", "worksheet",
]
TITLE_REJECT_RE = re.compile(
    r"(?:^|[\s\[(,.:;-])(?:sheet|plate|pl\.?|no\.?)\s*\d+", re.I)

# Descriptions that describe a thing which is not really a map.
DESC_REJECT = ["braille", "relief model", "globe gores"]

# How many maps we keep per category. The pool is committed to the repo
# and rewritten on every harvest, so it is worth keeping small; 1200 maps
# is over three years of daily picks in a single category and eleven
# years across the whole pool.
PER_CATEGORY_CAP = 1200

POOL_VERSION = 1

# --- how a map renders once the screen has dithered it ---
#
# Metadata cannot tell a legible map from a grey rectangle. Measuring
# can: fetch the map small, and look at how its tones fall.
#
#   mush   share of pixels that are neither ink nor paper. Mid greys are
#          what dithering turns into noise, and a scan that is almost
#          all mid grey arrives as a uniform stipple with nothing in it.
#   detail mean edge magnitude. Separates a blank sheet from a map, both
#          of which can have little mush.
#
# Mush was calibrated against 1-bit renders, where mid greys dither into
# noise. Almost every TRMNL panel is 2-bit or 4-bit, and at four grey
# levels a tonal map is simply a tonal map: a railroad map reading 94
# mush -- which the 1-bit test called unreadable -- is perfectly legible,
# title and all. So mush no longer rejects anything; it is kept only as
# a tiebreaker, and the bar is set where an image is uniform enough to
# have nothing in it at any bit depth.
UNREADABLE_MUSH = 99.0
UNREADABLE_DETAIL = 22.0

# Above this, the page is printed text rather than a map -- the index of
# an atlas rather than one of its plates.
TEXT_RATIO = 2.5

# Measuring costs one request per map, so a run measures at most this
# many new ones and remembers the answers in quality.json. The pool is
# covered after a few runs and re-measured never.
MEASURE_BUDGET = 1200
MEASURE_SIZE = "!800,480"


# ============================================================
# http
# ============================================================

def fetch_json(url):
    """GET a loc.gov URL, with backoff. Returns None if it never works."""
    delay = 5
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "replace")
            return json.loads(body)
        except urllib.error.HTTPError as e:
            # 429 means we are going too fast, 5xx means LOC is having a
            # moment. Both are worth waiting out. A 404 is not.
            if e.code in (429, 500, 502, 503, 504):
                sys.stderr.write(f"  http {e.code}, retry in {delay}s\n")
            else:
                sys.stderr.write(f"  http {e.code} on {url}\n")
                return None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
                ConnectionError, OSError, json.JSONDecodeError) as e:
            # Truncated responses and dropped connections are ordinary on a
            # long paged crawl; treat them like a 503 and try again.
            sys.stderr.write(f"  {type(e).__name__}: {e}, retry in {delay}s\n")
        if attempt < RETRIES - 1:
            time.sleep(delay)
            delay *= 2
    return None


# ============================================================
# field wrangling
# ============================================================

# The IIIF service id, e.g. service:gmd:gmd374:g3741:g3741p:rr002540.
# Everything about the image -- size, crop, grayscale -- is a suffix on
# this, so it is the only image field the pool needs to store.
IIIF_RE = re.compile(r"/image-services/iiif/(service:[^/]+)/")
DIMS_RE = re.compile(r"#h=(\d+)&w=(\d+)")
PCT_RE = re.compile(r"/full/pct:([\d.]+)/")
YEAR_RE = re.compile(r"\b(1[3-9]\d\d|20\d\d)\b")


def first(value):
    """LOC wraps almost everything in a list. Unwrap it."""
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def nice_case(text):
    """
    Search results are lowercased; the nested item block usually is not.
    When we only have the lowercase copy, title-case it, leaving anything
    that already has capitals alone.
    """
    text = (text or "").strip()
    if not text or text != text.lower():
        return text
    return re.sub(r"[A-Za-z]+('[A-Za-z]+)?",
                  lambda m: m.group(0).capitalize(), text)


def parse_year(record):
    """Best guess at a four-digit year, or 0."""
    item = record.get("item") or {}
    for candidate in (record.get("date"), first(item.get("date")),
                      first(record.get("dates")),
                      first(item.get("created_published"))):
        m = YEAR_RE.search(str(candidate or ""))
        if m:
            return int(m.group(1))
    return 0


def parse_image(record):
    """
    Pull the IIIF service id and the map's native pixel size out of the
    image_url list. LOC gives us sized derivatives whose fragment carries
    the dimensions (#h=888&w=1109) and whose path carries the scale
    (pct:12.5), so native size is dimensions / scale -- no extra request.
    """
    service, width, height = "", 0, 0
    for url in record.get("image_url") or []:
        m = IIIF_RE.search(url)
        if not m:
            continue
        service = service or m.group(1)
        dims = DIMS_RE.search(url)
        pct = PCT_RE.search(url)
        if not (dims and pct):
            continue
        scale = float(pct.group(1)) / 100.0
        if scale <= 0:
            continue
        w = int(round(int(dims.group(2)) / scale))
        h = int(round(int(dims.group(1)) / scale))
        if w * h > width * height:
            width, height = w, h
    return service, width, height


def clean_text(text, limit):
    """One tidy line, trimmed on a word boundary."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:.-") + "..."


# LOC descriptions are the catalogue note field: a run-on of everything the
# cataloguer recorded, useful notes and shelf bookkeeping alike. These are
# the notes that say nothing to somebody looking at the map on a wall.
NOTE_NOISE = re.compile(r"""^(?:
      aacr2
    | available\ also
    | description\ derived
    | digitized
    | imperfect
    | lc\ (?:copy|panoramic|civil\ war|railroad|maps)
    | library\ of\ congress
    | this\ item\ is\ in\ the
    | title\ (?:from|devised)
    | cover\ title
    | (?:cataloged|catalogued)\ from
    | perspective\ map\ not\ drawn\ to\ scale
    | indexed\ for\ points\ of\ interest
    | includes\ (?:ill\.|illus|index|advertisement|text|table\ of)
    | scale\ (?:ca\.|[\d~])
    | oriented\ with
    | (?:gift|purchase|transfer|deposit|acquired|copyright)\b
    | (?:copy|no|vol|sheet|plate)\.?\ *\d
    | in\ (?:upper|lower)\ (?:left|right)
    | stamped
    | signed
    | accompanied\ by
    | for\ sale\ by
    | vault
    | photocopy
    | copy\ imperfect
    | printed\ area
    | separately\ published
    | duplicate\ sheet
    | erratum
    | issued\ (?:also|as|under)
    | acquisitions?\ control
    | statement\ of\ responsibility
    | at\ head\ of\ title
    | includes\ notes\.?$
    | ms\.\ \(
    | \d+\ *(?:x|by)\ *\d+
    )""", re.I | re.X)

NOTE_NOISE_ANY = re.compile(r"""
      please\ contact
    | web\ site\ as\ a\ raster
    | \(\dnd\ ed\.\)
    | aacr2
    | \d+/\d+          # MARC field bookkeeping, e.g. "100; 651/1; 710/1"
    | serial\ no\.
    | cong\.,
    | acquisitions?\ control
    | \bpm\d+\b
    """, re.I | re.X)


# Split the note field into notes. A full stop only ends a note when the
# word in front of it is long enough not to be an abbreviation, which is
# what keeps "Looking from the n. e." and "Vogt, Lith, Milwaukee, Wis."
# in one piece instead of leaving a stray fragment on screen.
# A closing quote after the full stop ends a note whatever came before it,
# which is what separates a printer's imprint from the note that follows.
# Zero-width, so the full stop and any closing quote stay on the note they
# belong to instead of being eaten by the split.
NOTE_SPLIT = re.compile(
    r"(?:(?<=[a-z]{4}[.!?])|(?<=[.!?][\"\u201d\u2019')]))\s+(?=[\"'\[(A-Z])")


def split_notes(text):
    """
    Notes, each ending in a full stop. Splitting eats the punctuation, so
    it goes back on -- otherwise two kept notes run into each other and
    read as one broken sentence.
    """
    notes = []
    for note in NOTE_SPLIT.split(text):
        note = note.strip()
        if not note:
            continue
        if note[-1] not in ".!?\"')]\u201d":
            note += "."
        # A quote the split left unbalanced looks like a typo on screen;
        # the note reads fine without any quoting at all.
        if note.count('"') % 2:
            note = note.replace('"', "").strip()
        notes.append(note)
    return notes


def clean_description(text, limit):
    """
    Keep the notes that describe the map, drop the ones that describe the
    catalogue record. Notes are separated by full stops, so split there,
    filter, and refill up to the limit -- which means throwing out
    boilerplate makes room for a real sentence rather than losing one.
    """
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return ""
    notes = split_notes(text)

    kept = []
    for note in notes:
        note = note.strip(" ;")
        if len(note) < 8:
            continue
        if NOTE_NOISE.match(note) or NOTE_NOISE_ANY.search(note):
            continue
        kept.append(note)
        if sum(len(k) + 1 for k in kept) >= limit:
            break

    joined = " ".join(kept)
    if len(joined) <= limit:
        return joined
    # Prefer dropping a whole trailing note to cutting one mid-sentence.
    while len(kept) > 1 and len(" ".join(kept)) > limit:
        kept.pop()
    joined = " ".join(kept)
    return joined if len(joined) <= limit else clean_text(joined, limit)


def tidy_place(text):
    """
    Subject headings run broad to narrow with double dashes -- "New
    Hampshire--Manchester". On screen that reads better the way an address
    does, narrowest first.
    """
    text = clean_text(text, 70)
    if "--" not in text:
        return text[:60]
    parts = [p.strip() for p in text.split("--") if p.strip()]
    return ", ".join(reversed(parts))[:60]


# "Scale ca. 1:1,875,000." in the notes. The ratio is the part worth
# showing; the "ca." and the sentence around it are not.
SCALE_RE = re.compile(r"scale[^.]*?(1\s*:\s*[\d,. ]{3,})", re.I)

# Subject terms so broad they say nothing on a screen next to a map.
# Checked per subdivision, so "Railroads--United States--Maps" keeps only
# "Railroads" and the place, which the layout already shows, goes.
DULL_SUBJECTS = {"maps", "united states", "manuscript maps", "maps, manuscript",
                 "early works to 1800", "history", "cartography", "america",
                 "aerial views", "pictorial works", "description and travel"}


# LOC brackets text it inferred or expanded -- "N[ew] Y[ork], [1858]".
# True, but it reads as a typo on a screen, so the brackets go and the
# text they hold stays.
BRACKETS = re.compile(r"[\[\]]")


def tidy_published(text):
    text = BRACKETS.sub("", clean_text(text, 60)).strip()
    return re.sub(r"\s{2,}", " ", text).strip(" ,.;:")


def parse_scale(item):
    for note in (item.get("notes") or []):
        m = SCALE_RE.search(str(note))
        if m:
            return "1:" + m.group(1).split(":", 1)[1].strip().rstrip(".,")
    return ""


def parse_subjects(item, limit=4):
    """
    The specific parts of the subject headings. LCSH strings are
    subdivided with double dashes -- "Railroads--United States--Maps" --
    so each part is judged on its own and the boilerplate ones go.
    Search results give these as plain strings, the item endpoint as
    dicts; handle both.
    """
    out, seen = [], set()
    for subject in (item.get("subjects") or []):
        if isinstance(subject, dict):
            subject = next(iter(subject), "")
        parts = [p.strip() for p in str(subject or "").split("--")]
        parts = [p for p in parts if p and p.lower() not in DULL_SUBJECTS]
        if not parts:
            continue
        # Only the lead term. The subdivisions after it are places the
        # layout already shows as `place`, and keeping them produced
        # lines like "Railroads, Iowa, Iowa, Nebraska" once several
        # headings were joined together.
        head = parts[0]
        if head.lower() in seen:
            continue
        seen.add(head.lower())
        term = nice_case(clean_text(head, 52))
        if term:
            out.append(term)
        if len(out) >= limit:
            break
    return out


def item_id(record):
    """Stable short id, from the item URL LOC already treats as canonical."""
    url = record.get("id") or record.get("url") or ""
    m = re.search(r"/(?:item|resource)/([^/?#]+)", url)
    return m.group(1) if m else ""


# ============================================================
# filtering
# ============================================================

def evaluate(record, category, label):
    """
    Turn one search result into a pool entry, or into a rejection reason.
    Returns (entry, None) or (None, "reason").
    """
    if record.get("access_restricted"):
        return None, "access restricted"

    # A record whose resource holds several files is a sectioned map, an
    # atlas or a sketchbook, and the image handed back is the first file
    # -- which is the engraved title sheet rather than any of the map.
    # Rather than discard it (that cost most of the Atlantic Neptune and
    # an atlas of 272 city views), the section count is carried forward
    # and a later section is chosen further down, where measuring can
    # say whether it is worth showing.
    resource = (record.get("resources") or [{}])[0]
    sections = int(resource.get("files") or 1)
    if not record.get("digitized", True):
        return None, "not digitized"

    formats = [str(f).lower() for f in (record.get("original_format") or [])]
    if formats and "map" not in formats:
        return None, "not a map"
    online = [str(f).lower() for f in (record.get("online_format") or [])]
    if online and "image" not in online:
        return None, "no online image"

    ident = item_id(record)
    if not ident:
        return None, "no id"

    # Stored whole. LOC's own titles are never truncated -- one in this
    # collection runs to 431 characters -- so any ellipsis a reader sees
    # would be ours, and `title` is the field for people who want the
    # catalogue's words rather than a headline. title_short does the
    # trimming, deliberately and in one place.
    title = re.sub(r"\s+", " ", str(record.get("title") or "")).strip()
    if not title:
        return None, "no title"
    low = title.lower()
    if any(bad in low for bad in TITLE_REJECT) or TITLE_REJECT_RE.search(title):
        return None, "title looks like a fragment"

    year = parse_year(record)
    if not year or year < MIN_YEAR:
        return None, "no usable year"
    if year > MAX_YEAR:
        return None, f"after {MAX_YEAR}"

    service, width, height = parse_image(record)
    if not service:
        return None, "no IIIF image"
    if not width or not height:
        return None, "no image dimensions"
    if min(width, height) < MIN_SHORT_SIDE or width * height < MIN_PIXELS:
        return None, "image too small"
    aspect = width / float(height)
    if aspect < MIN_ASPECT or aspect > MAX_ASPECT:
        return None, "awkward aspect ratio"

    item = record.get("item") or {}
    description = clean_description(first(item.get("summary"))
                                    or first(record.get("description")), 220)
    if any(bad in description.lower() for bad in DESC_REJECT):
        return None, "not a flat map"

    creator = ""
    contributors = item.get("contributors")
    if isinstance(contributors, list) and contributors:
        head = contributors[0]
        creator = head if isinstance(head, str) else first(list(head))
    creator = nice_case(clean_text(creator, 70))

    place = nice_case(tidy_place(first(item.get("location"))
                                 or first(record.get("location"))))
    subjects = parse_subjects(item)

    entry = {
        "id": ident,
        "t": title,
        "y": year,
        "c": creator,
        "p": place,
        "d": description,
        "m": clean_text(first(item.get("medium")), 60),
        # Where and when it was published, which is often a different
        # place and year from the survey the map depicts.
        "pub": tidy_published(first(item.get("created_published"))),
        "sc": parse_scale(item),
        "subj": subjects,
        "k": category,
        "col": label,
        "g": topics_for(title, subjects, label),
        "s": service,
        "w": width,
        "h": height,
    }
    if sections > 1:
        entry["n"] = sections
    return entry, None


# The topics a reader might actually want to follow, matched against the
# title, the subject headings and the collection name -- deliberately not
# the description, which is catalogue prose and tags half the pool as
# nautical because a note mentions a harbour.
#
# A map carries as many topics as it matches (1.45 on average) and every
# map in the pool matches at least one. Only topics with roughly a year
# of daily use behind them are here; national parks (99 maps) and world
# maps (36) were dropped for repeating too fast to be worth offering.
TOPIC_PATTERNS = {
    "city-plans":       r"cities and towns|plan of the city|street map|city of ",
    "birds-eye-views":  r"bird'?s.?eye|perspective map|aerial view|panoramic",
    "civil-war":        r"civil war|1861-1865|confederate|rebel",
    "railroads":        r"rail ?road|railway",
    "roads-and-travel": r"turnpike|stage route|tourist|road map",
    "revolution":       r"revolutionary war|1775-1783",
    "land-ownership":   r"landowner|real property|cadastral",
    "battles-and-forts": r"\bbattle|fortificat|\bfort\b|siege|entrenchment|seat of war",
    "nautical":         r"nautical|harbou?r|\bchart\b|soundings|coast survey|lighthouse",
    "exploration":      r"discover|exploration|expedition|voyage",
    # Added once the World Digital Library maps arrived: 36 maps before
    # them, 712 after, which is the difference between a topic that
    # loops every five weeks and one that lasts two years.
    "world-and-hemispheres": (r"\bworld\b|hemisphere|globe|terrarum|"
                              r"universalis|mappe.?monde|orbis"),
}

# The same topics in Dutch, for the Rijksmuseum's titles. Without these
# every map from that source matched nothing and was reachable only
# through "All Maps" and the eras -- in a pool where every Library map
# carries at least one topic, that is a source hidden behind the one
# selection it was added to diversify.
#
# Kept apart from the patterns above rather than folded into them
# because two of the words collide with English: "kaart" is a map but
# "chart" is nautical, and "plan" is a city plan in Dutch where in the
# Library's titles it is as often a battle plan.
TOPIC_PATTERNS_NL = {
    "city-plans":       r"plattegrond|stadsplan|\bstad\b|plan van de stad",
    "birds-eye-views":  r"vogelvlucht|profiel van|gezicht op",
    "railroads":        r"spoorweg|spoorlijn",
    "roads-and-travel": r"wegenkaart|reiskaart|postroute",
    "land-ownership":   r"kadaster|grondeigendom|perceel",
    # Including the forms this catalogue actually uses for a campaign
    # map, which the first pass missed: an attack on a town, an army
    # camp, an order of battle, a raid. Those were sitting with no topic
    # at all despite being the clearest matches in the sample.
    "battles-and-forts": r"\bbeleg\b|belegering|veldslag|\bslag bij\b"
                         r"|vesting|fort\b|schans|aanval|legerkamp"
                         r"|slagorde|\btocht naar\b|\bkamp van\b",
    "nautical":         r"zeekaart|paskaart|haven|kust|loodsboek|rede van",
    "exploration":      r"ontdekking|expeditie|\breis\b|zeilagie|seylagie",
    "world-and-hemispheres": r"wereldkaart|werelddeel|halfrond|aardbol",
}
TOPIC_RES_NL = {name: re.compile(pat, re.I)
                for name, pat in TOPIC_PATTERNS_NL.items()}
TOPIC_RES = {name: re.compile(pat, re.I) for name, pat in TOPIC_PATTERNS.items()}


def topics_for(title, subjects, collection):
    text = " ".join([title, " ".join(subjects), collection])
    found = {name for name, rx in TOPIC_RES.items() if rx.search(text)}
    found |= {name for name, rx in TOPIC_RES_NL.items() if rx.search(text)}
    return sorted(found)


# Media that tend to mean "hand-drawn, and mostly blank paper".
SPARSE_MEDIUM = re.compile(
    r"\bms\.|manuscript|sketch|tracing (?:linen|cloth|paper)|pencil", re.I)


def score(entry):
    """
    Rough "will this look good on e-ink" score, used only to decide what
    to keep when a category overflows its cap. Big, roughly landscape,
    and described beats small, thin, and anonymous.
    """
    aspect = entry["w"] / float(entry["h"])
    points = 0.0
    # Detail to spare: more pixels means the downscale hides scan noise.
    points += min(entry["w"] * entry["h"] / 40_000_000.0, 1.0) * 40
    # Inside the band it fits every panel; outside, distance costs.
    off = max(ASPECT_BAND[0] - aspect, aspect - ASPECT_BAND[1], 0.0)
    points += max(0.0, 1.0 - off / 1.6) * 30
    if entry["d"]:
        points += 15
    if entry["c"]:
        points += 8
    if entry["p"]:
        points += 4
    # A century of engraved line work reads better than a 1920s halftone.
    if entry["y"] <= 1900:
        points += 3
    # One-off pen sketches on tracing linen are often three streets and a
    # lot of paper. Printed maps were engraved, so they carry more ink.
    # A penalty rather than a filter, because the best manuscript maps
    # (Sneden's Civil War drawings) are among the finest things here.
    if SPARSE_MEDIUM.search(entry["m"] + " " + entry["d"]):
        points -= 12
    return points


# ============================================================
# render quality
# ============================================================

def load_quality():
    try:
        with open(QUALITY_PATH) as fh:
            return json.load(fh).get("scores") or {}
    except (OSError, ValueError):
        return {}


def save_quality(scores):
    with open(QUALITY_PATH, "w") as fh:
        json.dump({"version": 1, "measured": len(scores), "scores": scores},
                  fh, separators=(",", ":"), sort_keys=True)
        fh.write("\n")


def measure(entry):
    """(mush, detail) for one map, or None if it could not be fetched."""
    try:
        from PIL import Image, ImageFilter
    except ImportError:
        return None
    base = (entry["s"] if "://" in entry["s"]
            else "https://tile.loc.gov/image-services/iiif/" + entry["s"])
    url = "{}/full/{}/0/gray.jpg".format(base, MEASURE_SIZE)
    delay = 3
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            raw = urllib.request.urlopen(req, timeout=45).read()
            break
        except Exception:
            if attempt == 2:
                return None
            time.sleep(delay)
            delay *= 2
    try:
        import io
        image = Image.open(io.BytesIO(raw)).convert("L")
    except Exception:
        return None
    pixels = list(image.getdata())
    total = len(pixels) or 1
    ink = sum(1 for p in pixels if p < 90) / total
    paper = sum(1 for p in pixels if p > 200) / total
    mush = (1.0 - ink - paper) * 100.0
    edges = image.filter(ImageFilter.FIND_EDGES).getdata()
    detail = sum(edges) / float(len(edges) or 1)

    # Printed text rules the page into horizontal lines, so its darkness
    # alternates far more down the page than across it. Maps sit near
    # 1.5, an index page at 3.5 and up. This is what stops a sectioned
    # atlas offering its index instead of a plate.
    width, height = image.size
    load = image.load()
    rows = [sum(load[x, y] for x in range(0, width, 2)) / (width / 2.0)
            for y in range(height)]
    cols = [sum(load[x, y] for y in range(0, height, 2)) / (height / 2.0)
            for x in range(width)]
    row_alt = sum(abs(rows[i + 1] - rows[i])
                  for i in range(len(rows) - 1)) / max(len(rows) - 1, 1)
    col_alt = sum(abs(cols[i + 1] - cols[i])
                  for i in range(len(cols) - 1)) / max(len(cols) - 1, 1)
    texty = row_alt / max(col_alt, 0.01)
    return round(mush, 1), round(detail, 1), round(texty, 2)


# The section number lives at the end of the last part of the service
# id -- ca000001, pa00001d, sb00001a -- so a sibling section is that
# number incremented, keeping the width and any letter after it.
SECTION_NUM = re.compile(r"(\d+)(\D*)$")


def section_variant(service, index):
    head, _, tail = service.rpartition(":")
    m = SECTION_NUM.search(tail)
    if not m:
        return None
    digits, suffix = m.group(1), m.group(2)
    renumbered = str(index).zfill(len(digits))
    if len(renumbered) != len(digits):
        return None
    return head + ":" + tail[:m.start(1)] + renumbered + suffix


def section_candidates(service, sections):
    """
    Sections worth trying, middle first. The front of an atlas is title
    page, dedication and index; the middle is where the plates are.
    """
    wanted, seen = [], set()
    for index in (sections // 2, sections // 2 + 1, 3, 2):
        if index < 2 or index > sections or index in seen:
            continue
        seen.add(index)
        variant = section_variant(service, index)
        if variant:
            wanted.append(variant)
    return wanted


def readable(score):
    """None means not measured yet, which is not held against a map."""
    if score is None:
        return True
    mush, detail = score[0], score[1]
    texty = score[2] if len(score) > 2 else 0.0
    return (mush < UNREADABLE_MUSH and detail > UNREADABLE_DETAIL
            and texty < TEXT_RATIO)


def render_rank(score):
    """
    Lower is better, for choosing what survives a category cap. Detail
    is what matters -- a map with more in it beats a sparse one -- with
    mush left as a mild tiebreaker rather than a judgement.
    """
    if score is None:
        return 50.0
    mush, detail = score[0], score[1]
    return -min(detail, 60.0) + mush * 0.15


# ============================================================
# harvest
# ============================================================

# ============================================================
# the Rijksmuseum
# ============================================================
#
# A second source, and the first that is not the Library of Congress.
#
# Why this one, out of everything an aggregator lists: the pool is 82%
# maps of somewhere in the United States, because six of the ten
# Library collections here are American by definition. Thinning that
# share (see crowded_share in daily.py) redistributes what is there; it
# cannot add Suriname.
#
# Europeana was the obvious place to look and turned out to be the wrong
# one. It aggregates metadata, not images: its own thumbnails cap at
# 400px, its IIIF manifests point back at whatever the provider hosts,
# and the providers behind the biggest map collections serve fixed JPEGs
# -- the National Library of Israel through a cache that 404s on nearly
# half of them, the British Library through Flickr, whose CDN
# rate-limits datacentre addresses. Both would have cost the panel the
# thing that makes this recipe work, which is asking for the exact
# derivative the screen needs.
#
# The Rijksmuseum serves real IIIF through Micrio, and the postcard
# recipe already resolves it, so the path is known to work in
# production rather than hoped to. Measured on the first map resolved
# here: 8216x5544, and full/!800,480/0/gray.jpg comes back 711x480 at
# 87KB.
#
# Dutch cartography also goes where this pool does not: Suriname,
# Languedoc, Java, Senegal, north-west Africa, the Batavian Republic.

RIJKS_SEARCH = "https://data.rijksmuseum.nl/search/collection"
RIJKS_CACHE = os.path.join(HERE, "rijks.json")

# The object types that are maps. "kaart" is the broad one and does mean
# card in other contexts, but filed as an object type against an image
# it is a map -- sampled, it gives Suriname, Languedoc, Haarlem and the
# siege of Breda. "plattegrond" is a ground plan, which is this pool's
# city-plans. "landkaart" and "atlas" exist and hold 27 between them,
# which is not worth a pass of its own.
RIJKS_TYPES = ("kaart", "plattegrond")

# Linked Art points at Getty AAT numbers rather than naming its fields.
AAT_PRIMARY_TITLE = "300404670"
AAT_OBJECT_NUMBER = "300312355"

RIJKS_IIIF_TAIL = re.compile(r"/full/[^/]+/\d+/\w+\.jpg$")
RIJKS_WORKERS = 4

# Resolving one map costs four requests -- object, visual item, digital
# object, info.json -- so a run pays for a slice and remembers it. The
# cache is committed, so the next monthly run starts where this one
# stopped and the pool grows without ever re-asking.
RIJKS_BUDGET = 400

# A sheet of a set, in Dutch. The English equivalents are in
# TITLE_REJECT already; these are the spellings this catalogue uses.
# "blad linksboven" is the top-left sheet of a wall map, which is a
# quarter of a map and reads as a fragment on a panel.
# A sheet of a set, in Dutch. The English equivalents are in
# TITLE_REJECT already.
#
# The part designation is in brackets -- "(deel linksboven)",
# "(onderste deel)" -- and that is what makes it safe to match on. The
# same words run descriptively in perfectly good titles: "Kaart een
# deel van de kust van Noorwegen", "Zeekaart van het oostelijk deel van
# de Middellandse zee". Matching the bare word rejected 18 whole maps
# along with the 70 fragments; matching it only inside brackets takes
# the fragments and leaves the maps.
RIJKS_TITLE_REJECT = re.compile(
    r"\([^)]*\b(?:deel|blad|bladen|stuk|gedeelte)\b[^)]*\)"
    r"|\bblad\b|\bbladen\b|titelpagina|titelblad|omslag|achterzijde"
    r"|\bkeerzijde\b|\bfragment\b|\bproefdruk\b", re.I)


def la_node(value):
    """
    One Linked Art node, whether the catalogue gave one or a list.

    Most records carry produced_by.timespan as a single object and some
    carry a list of them -- one in twenty-five in the first sample. The
    difference is invisible until a record with a list arrives, and then
    .get() raises on a list and takes the whole batch with it.
    """
    if isinstance(value, list):
        return value[0] if value else {}
    return value or {}


def rijks_fetch(url, accept="application/ld+json"):
    """One Linked Art node, or None."""
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": accept})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception:
            if attempt == 2:
                return None
            time.sleep(2 * (attempt + 1))
    return None


def la_names(node, aat=None):
    """The `identified_by` strings on a Linked Art node, optionally by type."""
    out = []
    for name in node.get("identified_by") or []:
        kinds = [c.get("id", "").rsplit("/", 1)[-1]
                 for c in (name.get("classified_as") or [])]
        if aat is None or aat in kinds:
            content = name.get("content")
            if content:
                out.append(" ".join(str(content).split()))
    return out


def rijks_iiif(record):
    """
    The IIIF endpoint for a map, three records deep.

      HumanMadeObject -> shows            -> VisualItem
      VisualItem      -> digitally_shown_by -> DigitalObject
      DigitalObject   -> access_point     -> the IIIF endpoint

    The VisualItem's own `digitally_shown_by` entry carries only an id,
    so the second hop is not optional. Stopping at the VisualItem's
    `subject_of` is the tempting mistake -- that one is the catalogue
    web page, not the image.
    """
    shows = la_node(record.get("shows")).get("id")
    if not shows:
        return None
    visual = rijks_fetch(shows)
    for shown in ((visual or {}).get("digitally_shown_by") or []):
        for point in shown.get("access_point") or []:
            if "iiif" in str(point.get("id") or ""):
                return RIJKS_IIIF_TAIL.sub("", point["id"])
        if not shown.get("id"):
            continue
        digital = rijks_fetch(shown["id"])
        for point in ((digital or {}).get("access_point") or []):
            if "iiif" in str(point.get("id") or ""):
                return RIJKS_IIIF_TAIL.sub("", point["id"])
    return None


def rijks_dimensions(base):
    """(width, height) from the IIIF service, which is the only place
    this catalogue states the pixels -- the object record gives
    millimetres of paper."""
    info = rijks_fetch(base + "/info.json", accept="application/json")
    if not isinstance(info, dict):
        return None
    try:
        return int(info["width"]), int(info["height"])
    except (KeyError, TypeError, ValueError):
        return None


def rijks_resolve(object_id, stats):
    """One map, or None with the reason counted."""
    record = rijks_fetch(object_id)
    if not isinstance(record, dict):
        stats["rijks: unreachable"] += 1
        return None

    title = (la_names(record, AAT_PRIMARY_TITLE) or la_names(record) or [""])[0]
    title = clean_text(title, 300)
    if not title:
        stats["rijks: no title"] += 1
        return None
    if RIJKS_TITLE_REJECT.search(title) or TITLE_REJECT_RE.search(title):
        stats["rijks: a sheet or a fragment"] += 1
        return None
    if any(word in title.lower() for word in TITLE_REJECT):
        stats["rijks: rejected title"] += 1
        return None

    span = la_node(la_node(record.get("produced_by")).get("timespan"))

    def year(key):
        found = re.search(r"(\d{4})", str(span.get(key) or ""))
        return int(found.group(1)) if found else None

    y1, y2 = year("begin_of_the_begin"), year("end_of_the_end")
    if y1 is None:
        stats["rijks: no date"] += 1
        return None
    if not MIN_YEAR <= y1 <= MAX_YEAR:
        stats["rijks: out of range"] += 1
        return None

    base = rijks_iiif(record)
    if not base:
        stats["rijks: no image"] += 1
        return None
    dims = rijks_dimensions(base)
    if not dims:
        stats["rijks: no dimensions"] += 1
        return None
    width, height = dims

    # The same gates the Library's maps pass, for the same reasons: a
    # scan under the panel's short side is upscaled before it is
    # dithered, and a map far outside the panel's aspect is a stripe.
    if min(width, height) < MIN_SHORT_SIDE or width * height < MIN_PIXELS:
        stats["rijks: too small"] += 1
        return None
    aspect = width / float(height)
    if not MIN_ASPECT <= aspect <= MAX_ASPECT:
        stats["rijks: wrong shape"] += 1
        return None

    number = (la_names(record, AAT_OBJECT_NUMBER) or [""])[0]
    entry = {
        "id": "rijks:" + (number or object_id.rsplit("/", 1)[-1]),
        "t": title,
        "y": y1,
        "c": "",
        "p": "",
        "d": "",
        "m": "",
        "pub": "",
        "sc": "",
        "subj": [],
        "k": "world",
        "col": "Rijksmuseum",
        "g": topics_for(title, [], "Rijksmuseum"),
        "s": base,
        "w": width,
        "h": height,
        "u": object_id,
    }
    if y2 and y2 != y1:
        entry["y2"] = y2
    return entry


def load_rijks():
    try:
        with open(RIJKS_CACHE) as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = {}
    cache.setdefault("ids", [])
    cache.setdefault("resolved", {})
    cache.setdefault("done", False)
    return cache


def save_rijks(cache):
    with open(RIJKS_CACHE, "w") as fh:
        json.dump(cache, fh, separators=(",", ":"), sort_keys=True)
        fh.write("\n")


def rijks_maps(budget, stats, max_pages=MAX_PAGES):
    """
    Two passes, like the postcard recipe's: listing identifiers is one
    cheap request per hundred and runs to completion, resolving them is
    four requests each and runs to a budget.
    """
    cache = load_rijks()
    if not cache["done"]:
        seen = set(cache["ids"])
        # Done only if every type paged all the way to the end. Marking
        # it done regardless is what a --pages 1 smoke test did on the
        # first run here: it listed 200 of 7,553 ids, wrote done, and
        # every later run skipped the listing and resolved from a list
        # that was 97% short. The budget made it look like progress.
        finished = True
        for kind in RIJKS_TYPES:
            url = (RIJKS_SEARCH + "?"
                   + urllib.parse.urlencode({"imageAvailable": "true",
                                             "type": kind}))
            page = 0
            while url and page < max_pages:
                data = rijks_fetch(url, accept="application/json")
                if data is None:
                    sys.stderr.write("  rijks: listing failed, pausing\n")
                    finished = False
                    break
                if page == 0:
                    total = (data.get("partOf") or {}).get("totalItems")
                    sys.stderr.write("  rijks: {} of type {}\n".format(total,
                                                                      kind))
                for item in data.get("orderedItems") or []:
                    if item.get("id") and item["id"] not in seen:
                        seen.add(item["id"])
                        cache["ids"].append(item["id"])
                nxt = data.get("next")
                url = nxt.get("id") if isinstance(nxt, dict) else nxt
                page += 1
                time.sleep(REQUEST_DELAY)
            if url:
                # Still a next page when the page budget ran out.
                finished = False
        cache["done"] = finished
        save_rijks(cache)

    todo = [i for i in cache["ids"] if i not in cache["resolved"]][:budget]
    left = len([i for i in cache["ids"] if i not in cache["resolved"]])
    if todo:
        sys.stderr.write("  rijks: resolving {} of {} unresolved\n"
                         .format(len(todo), left))
        # Per record, because one of them must not cost the rest. A
        # single list-shaped timespan raised inside a worker, the
        # exception came back out of pool.map, and 600 resolutions --
        # 40 minutes of somebody else's API -- were thrown away without
        # reaching save_rijks. A record that cannot be read is a record
        # to count and skip, like every other rejection here.
        def resolve(object_id):
            try:
                return rijks_resolve(object_id, stats)
            except Exception as exc:
                stats["rijks: unreadable record"] += 1
                sys.stderr.write("  rijks: {} raised {}: {}\n".format(
                    object_id, type(exc).__name__, str(exc)[:80]))
                return None

        with ThreadPoolExecutor(max_workers=RIJKS_WORKERS) as pool:
            for object_id, entry in zip(todo, pool.map(resolve, todo)):
                cache["resolved"][object_id] = entry
        save_rijks(cache)

    kept = [e for e in cache["resolved"].values() if e]
    sys.stderr.write("  rijks: {} maps from {} resolved ({} still to do)\n"
                     .format(len(kept), len(cache["resolved"]),
                             len(cache["ids"]) - len(cache["resolved"])))
    return kept


def harvest_collection(slug, category, label, max_pages, stats, extra=""):
    """Page through one collection, returning the entries that survive."""
    entries = []
    page = 1
    while page <= max_pages:
        url = ("https://www.loc.gov/collections/{}/?fo=json&c={}&sp={}"
               "&at=results,pagination{}".format(slug, PER_PAGE, page, extra))
        data = fetch_json(url)
        if data is None:
            sys.stderr.write(f"  giving up on {slug} at page {page}\n")
            break

        results = data.get("results") or []
        if not results:
            break
        for record in results:
            entry, reason = evaluate(record, category, label)
            if entry:
                entries.append(entry)
            else:
                stats[reason] += 1

        pagination = data.get("pagination") or {}
        total_pages = pagination.get("total") or 0
        sys.stderr.write("  {} page {}/{}: kept {}\n".format(
            slug, page, min(total_pages, max_pages) or "?", len(entries)))
        if total_pages and page >= total_pages:
            break
        page += 1
        time.sleep(REQUEST_DELAY)
    return entries


def build_pool(max_pages, rijks_budget=RIJKS_BUDGET):
    stats = Counter()
    by_id = {}
    # The Rijksmuseum first, so that where the same map reaches the pool
    # twice the Library's richer metadata wins on score below -- it
    # carries a place, a creator and subject headings where this source
    # carries a title and a date.
    if rijks_budget:
        for entry in rijks_maps(rijks_budget, stats, max_pages):
            by_id[entry["id"]] = entry
    for category, slug, label, extra in SOURCES:
        for entry in harvest_collection(slug, category, label, max_pages,
                                        stats, extra):
            # A map in two collections (a Civil War map is also a military
            # campaign map) keeps whichever entry scored higher, so the
            # better metadata wins and it can only be picked once.
            existing = by_id.get(entry["id"])
            if existing is None or score(entry) > score(existing):
                by_id[entry["id"]] = entry
        time.sleep(REQUEST_DELAY)

    # Measure how the survivors render, cheapest-first: anything already
    # measured costs nothing, and a run only pays for MEASURE_BUDGET new
    # ones. Metadata cannot tell a map from a grey rectangle, so this is
    # the only filter that knows what the screen will show.
    quality = load_quality()
    candidates = list(by_id.values())
    budget = [MEASURE_BUDGET]

    def scored(service):
        """Cached measurement for a service id, measuring if there is budget."""
        if service in quality:
            return quality[service]
        if budget[0] <= 0:
            return None
        result = measure({"s": service})
        budget[0] -= 1
        if result:
            quality[service] = list(result)
        return quality.get(service)

    # A sectioned record needs a section chosen before anything else can
    # be said about it. Try the middle of the volume first -- the front
    # is title page and index -- and take the first section that both
    # renders and is not a page of print.
    sectioned = [e for e in candidates if e.get("n")]
    if sectioned:
        sys.stderr.write("resolving {} sectioned records\n".format(len(sectioned)))
    for entry in sectioned:
        chosen = None
        for service in section_candidates(entry["s"], entry["n"]):
            if readable(scored(service)) and service in quality:
                chosen = service
                break
        entry.pop("n", None)
        if chosen:
            entry["s"] = chosen
        else:
            # Either every section was front matter, or the budget ran
            # out before we could look. Both mean "not this run".
            stats["no usable section"] += 1
            by_id.pop(entry["id"], None)
    if sectioned:
        save_quality(quality)

    candidates = list(by_id.values())
    unmeasured = [e for e in candidates if e["s"] not in quality]
    if unmeasured:
        # Measure the best-looking ones on paper first, so the maps most
        # likely to survive the cap are the ones that get judged.
        unmeasured.sort(key=score, reverse=True)
        todo = unmeasured[:max(budget[0], 0)]
        sys.stderr.write("measuring {} of {} unmeasured maps\n"
                         .format(len(todo), len(unmeasured)))
        for i, entry in enumerate(todo, 1):
            result = measure(entry)
            if result:
                quality[entry["s"]] = list(result)
            if i % 100 == 0:
                sys.stderr.write("  measured {}/{}\n".format(i, len(todo)))
                save_quality(quality)
        save_quality(quality)

    readable_candidates = []
    for entry in candidates:
        sc = quality.get(entry["s"])
        if readable(sc):
            readable_candidates.append(entry)
        else:
            stats["renders as grey mush"] += 1

    # Cap each category. Ranking is by how the map renders, falling back
    # to the metadata score to separate maps that render alike.
    kept = []
    for category in sorted({c for c, _, _, _ in SOURCES}):
        group = [e for e in readable_candidates if e["k"] == category]
        group.sort(key=lambda e: (render_rank(quality.get(e["s"])), -score(e)))
        if len(group) > PER_CATEGORY_CAP:
            stats["over category cap"] += len(group) - PER_CATEGORY_CAP
        kept.extend(group[:PER_CATEGORY_CAP])
    kept.sort(key=lambda e: e["id"])
    return kept, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=MAX_PAGES,
                        help="max pages per collection (100 records each)")
    parser.add_argument("--rijks-budget", type=int, default=RIJKS_BUDGET,
                        metavar="N",
                        help="resolve at most N new Rijksmuseum maps this "
                             "run; 0 skips the source. The listing is "
                             "cached, so successive runs continue where "
                             "the last stopped")
    parser.add_argument("--dry-run", action="store_true",
                        help="print stats without writing the pool")
    args = parser.parse_args()

    entries, stats = build_pool(args.pages, args.rijks_budget)
    if not entries:
        sys.stderr.write("harvested nothing; leaving the existing pool alone\n")
        return 1

    counts = Counter(e["k"] for e in entries)
    print("kept {} maps".format(len(entries)))
    for category, count in sorted(counts.items()):
        print("  {:<12} {}".format(category, count))
    print("dropped:")
    for reason, count in stats.most_common():
        print("  {:<28} {}".format(reason, count))

    if args.dry_run:
        return 0

    # A harvest that collapses to a fraction of the pool we already have
    # means LOC changed something, not that the maps disappeared. Keep the
    # old pool and let the daily job carry on with it.
    if os.path.exists(POOL_PATH):
        try:
            with open(POOL_PATH) as fh:
                previous = json.load(fh).get("maps") or []
        except (OSError, ValueError):
            previous = []
        if previous and len(entries) < len(previous) * 0.6:
            sys.stderr.write(
                "harvest returned {} maps vs {} in the current pool; "
                "refusing to shrink it\n".format(len(entries), len(previous)))
            return 1

    pool = {
        "version": POOL_VERSION,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "Library of Congress, Geography and Map Division",
        "rights": ("Free to use and reuse; no known restrictions. "
                   "Credit: Library of Congress, Geography and Map Division."),
        "count": len(entries),
        "categories": {c: counts[c] for c in sorted(counts)},
        "maps": entries,
    }
    os.makedirs(os.path.dirname(POOL_PATH), exist_ok=True)
    with open(POOL_PATH, "w") as fh:
        json.dump(pool, fh, separators=(",", ":"), sort_keys=True)
        fh.write("\n")
    print("wrote {}".format(os.path.relpath(POOL_PATH, os.getcwd())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
