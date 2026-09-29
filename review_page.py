#!/usr/bin/env python3
"""
Build the review page from review.json, thumbnails and all.

Written into docs/ so GitHub Pages serves it, which is what lets the
reminder link to something current: the page is a snapshot of a window
that moves every day, and one rebuilt only when somebody remembers to
ask is always the wrong fortnight.

Every thumbnail is embedded. The page has to work from a static host
with no image service behind it, and half the archives will not be
fetched cross-origin anyway.

Decisions leave by copy and paste rather than by any API: the page is
served from a CDN with nothing to write to. It emits one block carrying
the window it covers, so pasting a stale block can be refused instead of
quietly reverting later work.
"""
import base64
import io
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REVIEW = os.path.join(HERE, "review.json")
CURATION = os.path.join(HERE, "curation.json")
OUT_DIR = os.path.join(HERE, "docs")
THUMB_PX = 290
THUMB_QUALITY = 58
WORKERS = 8

KIND = {
    "map-of-the-day": dict(
        title="Map Queue", noun="map", nouns="maps",
        measure="B/kpx", repo="map_of_the_day",
        sort_label="Thinnest ink", near_label="Only near the bar"),
    "postcard-of-the-day": dict(
        title="Postcard Queue", noun="card", nouns="cards",
        measure="detail", repo="postcard_of_the_day",
        sort_label="Least detail", near_label="Only the mushiest"),
}


def thumbnail(url, agent):
    """One greyscale tile as a data URI, or "" if it would not come."""
    from PIL import Image
    try:
        request = urllib.request.Request(url, headers={"User-Agent": agent})
        raw = urllib.request.urlopen(request, timeout=60).read()
        image = Image.open(io.BytesIO(raw))
        image.load()
        image = image.convert("L")
        image.thumbnail((THUMB_PX, THUMB_PX), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=THUMB_QUALITY, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(
            buffer.getvalue()).decode()
    except Exception:
        return ""


def fetch_all(items, agent):
    """
    Every thumbnail, with the ones that did not come asked for again.

    A burst of several hundred requests is enough for an archive to
    start refusing, and the Library of Congress does: built from a
    runner, 275 of 460 of its tiles came back empty where the other
    three archives returned every one, and the same build from a laptop
    had lost 10 of 621. It is load, not the URLs.

    So: fewer at a time on each pass, and a pause between them. The
    refusals are not spread evenly -- they arrive once the burst has
    been going a while -- so a second ask after a rest recovers most of
    what a first ask lost. The last pass goes one at a time, which is
    slow and is why it only ever sees what two passes could not get.
    """
    todo = list(items)
    for attempt, (workers, rest) in enumerate(
            ((WORKERS, 0), (4, 5), (1, 15)), start=1):
        if not todo:
            break
        if rest:
            time.sleep(rest)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(
                lambda it: it.__setitem__(
                    "img", thumbnail(it.get("thumb") or "", agent)),
                todo))
        got = sum(1 for i in todo if i["img"])
        if got or attempt > 1:
            sys.stderr.write(
                "  thumbnails pass {}: {} of {}\n".format(attempt, got, len(todo)))
        todo = [i for i in todo if not i["img"]]
    return items


def seen_before():
    """Ids the last built page already showed, so a repeat pass opens on
    what has changed rather than on 600 items already looked at."""
    try:
        with open(os.path.join(OUT_DIR, "seen.json")) as fh:
            return set(json.load(fh))
    except (OSError, ValueError):
        return set()


def main():
    with open(REVIEW) as fh:
        review = json.load(fh)
    try:
        with open(CURATION) as fh:
            curation = json.load(fh)
    except (OSError, ValueError):
        curation = {}

    kind = KIND.get(review.get("kind"))
    if not kind:
        sys.stderr.write("review.json has no kind I know how to draw\n")
        return 1

    agent = "{}/1.0 (review page)".format(kind["repo"])
    items = review.get("items") or []
    previous = seen_before()

    items = fetch_all(items, agent)
    got = sum(1 for i in items if i["img"])
    missing = len(items) - got
    if missing > len(items) * 0.05:
        sys.stderr.write(
            "WARNING: {} of {} thumbnails never came. The page is still "
            "usable -- a card with no tile keeps its title and can still "
            "be vetoed -- but that much missing is an archive refusing "
            "the load, not a handful of dead scans.\n"
            .format(missing, len(items)))
    slim = [{
        "id": i["id"],
        "t": i.get("title") or "",
        "y": str(i.get("year") or i.get("date") or ""),
        "b": i.get("byline") or i.get("place") or "",
        "d": i.get("density") if "density" in i else i.get("score"),
        "src": i.get("source") or "",
        "lang": i.get("lang") or "",
        "w": (i.get("when") or [])[:3],
        "n": len(i.get("when") or []),
        "new": i["id"] not in previous,
        "img": i["img"],
    } for i in items]

    payload = json.dumps({
        "kind": review.get("kind"),
        "from": review.get("from"), "to": review.get("to"),
        "generated": review.get("generated"),
        "anyold": bool(previous),
        "vetoed": sorted(curation.get("vetoed") or []),
        "flagged": sorted(curation.get("untranslate") or []),
        "labels": kind,
        "items": slim,
    }, separators=(",", ":"))

    with open(os.path.join(HERE, "review_page.html")) as fh:
        shell = fh.read()

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "index.html"), "w") as fh:
        fh.write(shell.replace("__DATA__", payload))
    with open(os.path.join(OUT_DIR, "seen.json"), "w") as fh:
        json.dump(sorted(i["id"] for i in items), fh)
    with open(os.path.join(OUT_DIR, ".nojekyll"), "w") as fh:
        fh.write("")

    size = os.path.getsize(os.path.join(OUT_DIR, "index.html"))
    sys.stderr.write(
        "{} {} ({} thumbnails, {} new) -> docs/index.html, {:.1f}MB\n".format(
            len(slim), kind["nouns"], got,
            sum(1 for i in slim if i["new"]), size / 1024.0 / 1024.0))
    if size > 60 * 1024 * 1024:
        sys.stderr.write("page is too large for a static host; refusing\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
