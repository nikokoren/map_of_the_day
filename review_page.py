#!/usr/bin/env python3
"""
Build the review page from review.json, thumbnails and all.

Written into docs/ so GitHub Pages serves it, which is what lets the
reminder link to something current: the page is a snapshot of a window
that moves every day, and one rebuilt only when somebody remembers to
ask is always the wrong fortnight.

Tiles are linked, not embedded, and the reader's browser fetches them
straight from the archive.

They used to be downloaded here and inlined as data URIs, on the grounds
that a static host has no image service behind it and the archives would
refuse a cross-origin fetch. The second half was simply wrong -- all
four send Access-Control-Allow-Origin: *, and a plain <img> needs no
such permission in the first place -- and the first half cost more than
it bought. Building the page meant several hundred image downloads in a
burst, and the Library of Congress throttles exactly that: built on a
runner it returned 33 of 452 tiles while the other three archives
returned every one, so two thirds of the page came out blank. Run from a
laptop the same code got all of them, which is why the retries that were
meant to fix it looked like they had.

Linking moves the fetch to a browser on an ordinary connection asking
for one picture at a time, which is the request these archives are built
to serve. It also takes the page from seven megabytes to a couple of
hundred kilobytes, so it opens on a phone, and stops a multi-megabyte
binary being committed to the repository every morning.

Decisions leave by copy and paste rather than by any API: the page is
served from a CDN with nothing to write to. It emits one block carrying
the window it covers, so pasting a stale block can be refused instead of
quietly reverting later work.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REVIEW = os.path.join(HERE, "review.json")
CURATION = os.path.join(HERE, "curation.json")
OUT_DIR = os.path.join(HERE, "docs")

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

    items = review.get("items") or []
    previous = seen_before()

    # A row with no image URL is a scan the archive never gave us a
    # thumbnail for. It still gets a tile with its title and can still be
    # vetoed; it is only worth a word if there are many of them, which
    # would mean the queue was built wrong rather than the page drawn
    # wrong.
    got = sum(1 for i in items if i.get("thumb"))
    missing = len(items) - got
    if missing > len(items) * 0.05:
        sys.stderr.write(
            "WARNING: {} of {} rows carry no image URL at all; that is the "
            "queue missing them, not the page.\n".format(missing, len(items)))
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
        "img": i.get("thumb") or "",
    } for i in items]

    payload = json.dumps({
        "kind": review.get("kind"),
        "from": review.get("from"), "to": review.get("to"),
        "generated": review.get("generated"),
        "anyold": bool(previous),
        "vetoed": sorted(curation.get("vetoed") or []),
        "flagged": sorted(curation.get("untranslate") or []),
        "starred": sorted(curation.get("starred") or []),
        "progress": review.get("progress") or {},
        "star_min": review.get("star_min") or 0,
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
        "{} {} ({} with a tile, {} new) -> docs/index.html, {:.0f}KB\n".format(
            len(slim), kind["nouns"], got,
            sum(1 for i in slim if i["new"]), size / 1024.0))
    if size > 60 * 1024 * 1024:
        sys.stderr.write("page is too large for a static host; refusing\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
