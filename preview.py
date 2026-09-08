#!/usr/bin/env python3
"""
Renders upcoming picks as a contact sheet at the panel's grey depth, so
a person can judge whether the pool is interesting.

Depth matters more than it sounds: at 1-bit everything mid-toned turns
to noise and half the collection looks broken, while at the 2-bit and
4-bit depths the panels actually have, the same maps read cleanly.
Default is 2-bit, the conservative case.

Nothing here measures anything. Legibility can be measured; whether a
map is worth looking at cannot, and this is the cheapest way to put the
question in front of someone who can answer it.

    python3 preview.py                 # the next 12 days
    python3 preview.py --days 24
    python3 preview.py --topic railroads
"""

import argparse
import io
import json
import os
import sys
import urllib.request
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daily  # noqa: E402

UA = "mission-control-trmnl/1.0 (github.com/nikokoren)"
TILE_W, TILE_H = 330, 210
COLS = 4


def fetch(service, width, height):
    from PIL import Image
    url = "https://tile.loc.gov/image-services/iiif/{}/full/!{},{}/0/gray.jpg".format(
        service, width, height)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        raw = urllib.request.urlopen(req, timeout=60).read()
        return Image.open(io.BytesIO(raw)).convert("L")
    except Exception:
        return None


def main():
    from PIL import Image, ImageDraw
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=12)
    parser.add_argument("--topic", default="all")
    parser.add_argument("--out", default="preview.png")
    parser.add_argument("--levels", type=int, default=4,
                        help="grey levels: 2, 4 (2-bit) or 16 (4-bit)")
    args = parser.parse_args()

    pool = daily.load_pool()
    if pool is None:
        return 1
    entries = daily.maps_for(pool["maps"], args.topic)
    if not entries:
        sys.stderr.write("no maps for topic {}\n".format(args.topic))
        return 1

    today = date.today()
    tiles = []
    for offset in range(args.days):
        day = today + timedelta(days=offset)
        entry = daily.pick(entries, args.topic, day, check=False)[0]
        image = fetch(entry["s"], TILE_W, TILE_H)
        tile = Image.new("L", (TILE_W, TILE_H + 26), 255)
        if image:
            tile.paste(image, ((TILE_W - image.size[0]) // 2, 0))
        caption = "{}  {}".format(entry["y"], daily.title_line(entry))
        ImageDraw.Draw(tile).text((3, TILE_H + 8), caption[:62], fill=0)
        tiles.append(tile.quantize(colors=args.levels,
                                   dither=Image.Dither.FLOYDSTEINBERG))
        sys.stderr.write("  {} {}\n".format(day, entry["t"][:60]))

    rows = (len(tiles) + COLS - 1) // COLS
    sheet = Image.new("L", (TILE_W * COLS, (TILE_H + 26) * rows), 255)
    for i, tile in enumerate(tiles):
        sheet.paste(tile.convert("L"),
                    ((i % COLS) * TILE_W, (i // COLS) * (TILE_H + 26)))
    sheet.save(args.out)
    print("wrote {} ({} days, topic {})".format(args.out, args.days, args.topic))
    return 0


if __name__ == "__main__":
    sys.exit(main())
