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
import urllib.request
from collections import Counter

# ============================================================
# config
# ============================================================

HERE = os.path.dirname(os.path.abspath(__file__))
POOL_PATH = os.path.join(HERE, "pool.json")

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

    title = clean_text(record.get("title"), 200)
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
TOPIC_RES = {name: re.compile(pat, re.I) for name, pat in TOPIC_PATTERNS.items()}


def topics_for(title, subjects, collection):
    text = " ".join([title, " ".join(subjects), collection])
    return [name for name, rx in TOPIC_RES.items() if rx.search(text)]


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
# harvest
# ============================================================

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


def build_pool(max_pages):
    stats = Counter()
    by_id = {}
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

    # Cap each category, best first, then sort the survivors by id so the
    # file is stable between harvests and the diff stays readable.
    kept = []
    for category in sorted({c for c, _, _, _ in SOURCES}):
        group = [e for e in by_id.values() if e["k"] == category]
        group.sort(key=score, reverse=True)
        if len(group) > PER_CATEGORY_CAP:
            stats["over category cap"] += len(group) - PER_CATEGORY_CAP
        kept.extend(group[:PER_CATEGORY_CAP])
    kept.sort(key=lambda e: e["id"])
    return kept, stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=MAX_PAGES,
                        help="max pages per collection (100 records each)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print stats without writing the pool")
    args = parser.parse_args()

    entries, stats = build_pool(args.pages)
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
