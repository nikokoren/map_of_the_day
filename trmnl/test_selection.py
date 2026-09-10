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

# image, title_short, year, creator, published, description_short,
# place, category_label, item_id
def card(title, image):
    return [image, title, "1866", "Fox, Stanley", "New York, 1866",
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
    str(DAY - 1): {k: ["IMG-y", "YESTERDAY " + v[1]] + v[2:]
                   for k, v in _cells.items()},
    str(DAY):     dict(_cells),
    str(DAY + 1): {k: ["IMG-t", "TOMORROW " + v[1]] + v[2:]
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
    "pick_fields": ["image", "title_short", "year", "creator", "published",
                    "description_short", "place", "category_label", "item_id"],
    "days": days,
}

# Local time decides the day, so each timezone case needs a clock that
# actually straddles a midnight somewhere.
#   EVENING 20:00 UTC on DAY -- Auckland (+12) is already on DAY+1.
#   EARLY   02:00 UTC on DAY -- Los Angeles (-7) is still on DAY-1.
EVENING = DAY * 86400 + 20 * 3600
EARLY = DAY * 86400 + 2 * 3600

OFFSETS = {"Auckland, evening UTC": 12 * 3600, "Berlin, evening UTC": 7200,
           "Los Angeles, early UTC": -7 * 3600, "Berlin, early UTC": 7200,
           "device clock missing": 7200}
CLOCKS = {"Auckland, evening UTC": EVENING, "Berlin, evening UTC": EVENING,
          "Los Angeles, early UTC": EARLY, "Berlin, early UTC": EARLY,
          "device clock missing": None}
EXPECT = {"Auckland, evening UTC": "TOMORROW", "Berlin, evening UTC": "",
          "Los Angeles, early UTC": "YESTERDAY", "Berlin, early UTC": "",
          "device clock missing": ""}

CASES = [
    ("Auckland, evening UTC", {}),
    ("Berlin, evening UTC", {}),
    ("Los Angeles, early UTC", {}),
    ("Berlin, early UTC", {}),
    ("device clock missing", {}),
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
    bad += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {name:26s} {tail}")

sys.exit(1 if bad else 0)
