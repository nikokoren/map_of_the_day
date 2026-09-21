"""
Render selection.liquid against a stand-in feed and a stand-in settings
blob, and check that every case resolves to a pick.

The Liquid is the one part of this that cannot be reasoned about
reliably. Every bug it has had was found by running it and by nothing
else: a filter inside a bracket index is a syntax error, `split` leaves
a trailing empty string that indexes into nothing, a feed key collides
with a settings keyname of the same name, and a boolean arrives as the
string "false".

    pip install python-liquid && python3 trmnl/test_selection.py
"""

import os
import re
import sys
from liquid import Environment

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "selection.liquid")).read()

env = Environment()
try:
    tmpl = env.from_string(src)
except Exception as exc:
    print("PARSE FAIL:", exc)
    sys.exit(1)
print("parse ok")

DAY = 20706
TS = 1789031899          # a timestamp from a real device dump

# image, title_short, year, byline, description_short, place,
# category_label, item_id -- the same order daily.PICK_FIELDS names, and
# the assertion below keeps it that way.
def card(title, image):
    return [image + "/full/!1872,1404/0/default.jpg", title, "1866", "by Stanley Fox in New York, 1866",
            "A line of context.", "New York", "Railroads", "12345"]

_cells = {
    "all":                          card("Everything", "IMG-all"),
    "railroads":                    card("Railroads", "IMG-rr"),
    "city-plans":                   card("City Plans", "IMG-cp"),
    "era-1850-1869":                card("1850-1869", "IMG-e1"),
    "railroads__era-1850-1869":     card("RR 1850s", "IMG-x"),
}

# One file, three days. Yesterday and tomorrow carry marker titles so a
# test can tell which day the markup actually landed on.
days = {
    str(DAY - 1): {k: ["IMG-y/full/!1872,1404/0/default.jpg",
                             "YESTERDAY " + v[1]] + v[2:]
                   for k, v in _cells.items()},
    str(DAY):     dict(_cells),
    str(DAY + 1): {k: ["IMG-t/full/!1872,1404/0/default.jpg",
                             "TOMORROW " + v[1]] + v[2:]
                   for k, v in _cells.items()},
}

feed = {
    "default_day": str(DAY),
    "day_index": DAY,
    "cell_keys": ["railroads__era-1850-1869"],
    "keys_by_label": {
        "railroads": "railroads", "Railroads": "railroads",
        "city_plans": "city-plans", "City Plans": "city-plans",
        "1850_-_1869": "era-1850-1869", "1850 - 1869": "era-1850-1869",
        "the_1700s": "era-1700s",
    },
    "themes": [], "eras": [],
    "pick_fields": ["image", "title_short", "year", "byline",
                    "description_short", "place", "category_label", "item_id"],
    "image_boxes": [{"width": 1040, "box": "1872,1404"},
                    {"width": 800, "box": "800,480"}],
    "image_box_default": "1872,1404",
    "image_suffix": "/0/default.jpg",
    "days": days,
}

# Local time decides the day, so each timezone case needs a clock that
# actually straddles a midnight somewhere.
#   EVENING 20:00 UTC on DAY -- Auckland (+12) is already on DAY+1.
#   EARLY   02:00 UTC on DAY -- Los Angeles (-7) is still on DAY-1.
#   LATE    23:00 UTC on DAY -- Berlin (+2) is on DAY+1 while UTC is not.
#           That last one is the gap the rotation used to fall into: the
#           day's picks moved on at local midnight and the cell chosen
#           out of them did not, so a viewer with more than one cell in
#           rotation got last night's map again for two hours.
EVENING = DAY * 86400 + 20 * 3600
EARLY = DAY * 86400 + 2 * 3600
LATE = DAY * 86400 + 23 * 3600

OFFSETS = {"Auckland, evening UTC": 12 * 3600, "Berlin, evening UTC": 7200,
           "Los Angeles, early UTC": -7 * 3600, "Berlin, early UTC": 7200,
           "device clock missing": 7200,
           "Berlin, past local midnight": 7200}
CLOCKS = {"Auckland, evening UTC": EVENING, "Berlin, evening UTC": EVENING,
          "Los Angeles, early UTC": EARLY, "Berlin, early UTC": EARLY,
          "device clock missing": None,
          "Berlin, past local midnight": LATE}
EXPECT = {"Auckland, evening UTC": "TOMORROW", "Berlin, evening UTC": "",
          "Los Angeles, early UTC": "YESTERDAY", "Berlin, early UTC": "",
          "device clock missing": "",
          "Berlin, past local midnight": "TOMORROW"}

# Where a case pins the cell as well as the day. city-plans and railroads
# rotate two-wide: DAY lands on the first, DAY+1 on the second.
EXPECT_KEY = {"Berlin, past local midnight": "railroads"}

CASES = [
    ("Auckland, evening UTC", {}),
    ("Berlin, evening UTC", {}),
    ("Los Angeles, early UTC", {}),
    ("Berlin, early UTC", {}),
    ("device clock missing", {}),
    ("Berlin, past local midnight", {"themes": ["city_plans",
                                                "railroads"]}),
    ("nothing selected", {}),
    ("themes only", {"themes": ["railroads"]}),
    ("eras only", {"eras": ["1850_-_1869"]}),
    ("both, cell exists", {"themes": ["railroads"], "eras": ["1850_-_1869"]}),
    ("string not array", {"themes": "city_plans"}),
    ("unknown value", {"themes": ["atlantis"]}),
    ("era that never existed", {"themes": ["railroads"],
                                "eras": ["the_1700s"]}),
    ("description off", {"show_description": "false"}),
    ("credit on", {"show_credit": "true"}),
]

probe = src + ("\n<<{{ chosen_key }}|{{ map_title }}|{{ map_image }}"
               "|c{{ want_caption }}|d{{ want_description }}"
               "|r{{ want_credit }}>>")
checked = env.from_string(probe)

bad = 0
for name, settings in CASES:
    ctx = dict(feed)
    ctx["trmnl"] = {
        "plugin_settings": {"custom_fields_values": settings},
        "device": {"width": 800, "height": 480},
        "system": {"timestamp_utc": CLOCKS.get(name, TS)},
        "user": {"utc_offset": OFFSETS.get(name, 7200)},
    }
    out = checked.render(**ctx)
    tail = out[out.rfind("<<") + 2:out.rfind(">>")]
    ok = "|" in tail and not tail.startswith("|")
    want = EXPECT.get(name)
    if ok and want is not None:
        title = tail.split("|")[1]
        ok = title.startswith(want) if want else not (
            title.startswith("YESTERDAY") or title.startswith("TOMORROW"))
    want_key = EXPECT_KEY.get(name)
    if ok and want_key is not None:
        ok = tail.split("|")[0] == want_key
    bad += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {name:26s} {tail}")

# ---------------------------------------------------------------
# The plugin runs example-markup.liquid, not this file. They hold the
# same selection logic because a private plugin has no {% include %},
# and for three days in September they did not: the local-midnight
# rotation was fixed here and nowhere else, so the fix never reached a
# device. Everything above tests the wrong file if these two drift.
# ---------------------------------------------------------------

def logic_lines(text):
    text = re.sub(r"\{%-?\s*comment\s*-?%\}.*?\{%-?\s*endcomment\s*-?%\}",
                  "", text, flags=re.S)
    return [line.strip() for line in text.splitlines() if line.strip()]


mine = logic_lines(src)
theirs = logic_lines(open(os.path.join(HERE, "example-markup.liquid")).read())
if theirs[:len(mine)] == mine:
    print("  ok   example-markup.liquid carries this exact logic")
else:
    bad += 1
    print("  FAIL example-markup.liquid has drifted from selection.liquid")
    for n, (a, b) in enumerate(zip(mine, theirs)):
        if a != b:
            print(f"       first difference at logic line {n}")
            print(f"         selection.liquid     {a[:76]}")
            print(f"         example-markup.liquid {b[:76]}")
            break


# The fixture's pick shape has to be the real one, or every test above is
# rehearsing a payload that daily.py does not produce.
sys.path.insert(0, os.path.dirname(HERE))
import daily  # noqa: E402
# Markup already installed on a device reads column 0 straight into src.
# It has no image_boxes, no split, none of the logic below -- it just
# uses the cell. So whatever column 0 holds has to be a URL that renders
# an image on its own, or every panel still running the published
# version goes blank the moment a new feed lands. It did, for half an
# hour: column 0 briefly held a bare IIIF base, which is a 302 to HTML.
for day, picks in feed["days"].items():
    for cell, row in picks.items():
        cell0 = row[0]
        ok = "/full/!" in cell0 and cell0.endswith(".jpg")
        if not ok:
            print(f"  FAIL column 0 of {cell} on {day} is not a usable src: "
                  f"{cell0!r}")
            bad += 1
            break
    else:
        continue
    break
else:
    print("  ok   column 0 renders on its own, for markup already installed")

# The box each panel asks for. This is the one place markup is allowed
# to build a URL, and it may only build one the daily job has warmed --
# so every answer here has to be a box in image_boxes, or the default.
# An OG asking for the X box is 5.5x the dither work for a picture it
# cannot show, and asking for anything off the list is a blank panel.
BOXES = [
    ("an OG",               800,  "800,480"),
    ("an X",               1040,  "1872,1404"),
    ("a panel smaller than either", 600, "800,480"),
    ("a panel wider than either",  1400, "1872,1404"),
    ("no width at all",    None,  "1872,1404"),
]
warmed = {b["box"] for b in feed["image_boxes"]} | {feed["image_box_default"]}
for name, width, want in BOXES:
    ctx = dict(feed)
    ctx["trmnl"] = {
        "plugin_settings": {"custom_fields_values": {}},
        "device": ({"width": width, "height": 480} if width else {}),
        "system": {"timestamp_utc": TS},
        "user": {"utc_offset": 7200},
    }
    tail = checked.render(**ctx)
    tail = tail[tail.rfind("<<") + 2:tail.rfind(">>")]
    got = tail.split("|")[2]
    box = got.split("/full/!")[-1].rsplit("/0/", 1)[0]
    ok = box == want and box in warmed
    print(("  ok   " if ok else "  FAIL ") + f"{name} asks for !{box}"
          + ("" if ok else f", wanted !{want}"))
    bad += 0 if ok else 1

if list(daily.PICK_FIELDS) == feed["pick_fields"]:
    print("  ok   the stand-in feed uses daily.PICK_FIELDS")
else:
    bad += 1
    print("  FAIL the stand-in feed's pick_fields have drifted from daily.py")
    print(f"       daily.py {list(daily.PICK_FIELDS)}")
    print(f"       fixture  {feed['pick_fields']}")

sys.exit(1 if bad else 0)
