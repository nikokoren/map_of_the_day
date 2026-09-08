# Map of the Day

One historical map a day, from the Library of Congress, on a TRMNL screen.
No API key, no server, nothing for the person installing it to maintain.

```
MAP OF THE DAY
Railroad map of New Hampshire
1894 - New Hampshire. Railroad Commissioners
[the map, filling the screen]
```

```
harvest.py              builds the candidate pool from the Library of Congress
daily.py                picks the day's map and writes what TRMNL polls
pool.json               the vetted candidates (rewritten monthly)
map.json                today's map, all categories -- the default polling URL
today/*.json            today's map within one category
sources.md              what was checked about the other image-of-the-day sources
.github/workflows/      the two jobs that run the above on a schedule
```

## How it works

Three moving parts, and only one of them touches the internet on a normal day:

| When | What runs | What it writes |
|---|---|---|
| Monthly | `harvest.py` (Refresh Map Pool workflow) | `pool.json` -- a few thousand vetted maps |
| Daily, 00:05 UTC | `daily.py` (Map of the Day workflow) | `map.json` and `today/*.json` |
| Every device refresh | TRMNL polls the raw file | the screen |

The daily pick is a pure function of the pool and the date, so it needs no
network access to decide anything: `sha256(salt + category + cycle + item id)`
gives each category a stable shuffle, and the day number indexes into it. Two
consequences that matter:

- **The map cannot change during the day.** Re-running the job, a device
  polling twice, a second device in another room -- all get the same map,
  because they all read the same committed file.
- **A bad day at loc.gov cannot blank the screen.** The pool is already in
  the repo. If the harvest fails, the daily job carries on with last month's
  pool. If the daily job fails, yesterday's `map.json` is still there and
  TRMNL keeps rendering it.

This is also the answer to the Cloudflare problem: TRMNL never talks to
loc.gov. GitHub Actions does the talking, once a month, from a runner that
loc.gov serves normally. TRMNL only ever fetches a static JSON file from
`raw.githubusercontent.com` and an image from `tile.loc.gov`.

## TRMNL setup

1. **New Private Plugin**, strategy **Polling**.
2. **Polling URL**:
   ```
   https://raw.githubusercontent.com/nikokoren/map_of_the_day/main/map.json
   ```
   No headers, no auth, no body.
3. Write the markup against the fields below.
4. Save. The screen updates itself from then on.

The payload's keys arrive at the top of the template context, the same way
`launch.json` does for the mission control plugin -- `{{ title }}`,
`{{ image }}`, and so on.

### Categories, if you want them

The daily job writes one file per category, so a category setting is just a
different URL rather than a different code path:

| Setting value | Polling URL suffix |
|---|---|
| All maps | `map.json` |
| Cities & towns | `today/cities.json` |
| Panoramic views | `today/panoramas.json` |
| Railroads | `today/railways.json` |
| Battles & campaigns | `today/military.json` |
| Discovery & exploration | `today/exploration.json` |
| National parks | `today/nature.json` |

Every file has identical fields, so one template covers all of them. In a
private plugin you can add a `select` custom field keyed `category` and
interpolate it into the polling URL
(`.../main/today/{{ category }}.json`); check the interpolation renders
before publishing, and if it does not, ship the all-maps URL first and add
the setting later. Nothing else about the recipe changes either way.

## Fields

| Field | Example | Notes |
|---|---|---|
| `heading` | `MAP OF THE DAY` | fixed label, so the layout has no hardcoded copy |
| `title` | `Railroad map of New Hampshire accompanying report...` | full LOC title |
| `title_short` | `Railroad map of New Hampshire` | trimmed at the subtitle, for a headline |
| `year` | `1894` | four digits, always present |
| `creator` | `New Hampshire. Railroad Commissioners` | may be empty |
| `place` | `New Hampshire` | may be empty |
| `collection` | `Railroad Maps, 1828-1900` | the LOC collection it came from |
| `description` | `Township and county map showing relief by hachures...` | trimmed to ~220 chars, may be empty |
| `medium` | `col. map 52 x 40 cm.` | the physical object |
| `byline` | `New Hampshire. Railroad Commissioners - 1894` | creator and year, pre-joined |
| `subtitle` | `New Hampshire - Railroad Maps, 1828-1900` | place and collection, pre-joined |
| `image` | `https://tile.loc.gov/.../full/!800,480/0/gray.jpg` | grayscale, pre-fitted to the panel |
| `image_large` | `.../full/!1600,960/0/gray.jpg` | for a higher-resolution panel |
| `image_color` | `.../full/!800,480/0/default.jpg` | same size, original colour |
| `thumb` | `.../full/!320,320/0/gray.jpg` | for a mashup layout |
| `image_width`, `image_height` | `4784`, `6488` | the scan's native size |
| `aspect`, `orientation` | `0.737`, `portrait` | pick a layout without measuring |
| `source`, `credit`, `rights` | `Library of Congress, Geography and Map Division` | attribution line |
| `item_url` | `https://www.loc.gov/item/98688514/` | the record, for a QR code or footer |
| `date`, `category`, `category_label` | `2026-09-08`, `railways`, `Railroads` | what this file is |
| `pool_size`, `pool_generated`, `generated` | | diagnostics |
| `image_checked`, `image_bytes` | `ok`, `61550` | what the daily check found, and how heavy the fitted JPEG is |

Images come from the Library's IIIF service. `!800,480` means "fit inside
this box without distorting", and `gray.jpg` asks the Library's server for
the greyscale conversion, so the panel dithers a picture that is already the
right shape and colour space. No map is ever upscaled past its own scan.

## Running it by hand

```bash
python3 daily.py                    # write today's files
python3 daily.py --dry-run          # print the picks only
python3 daily.py --preview 14       # the next fortnight
python3 daily.py --date 2026-12-25  # any particular day
python3 daily.py --selftest         # check the schedule holds
python3 harvest.py --pages 2 --dry-run   # smoke-test the harvest
python3 harvest.py                  # full harvest, ~10 minutes
```

`--selftest` checks the properties the schedule is supposed to have against
the real pool: same day gives the same map, one pass through the pool repeats
nothing, the next pass comes out in a different order, every category is deep
enough to offer as a setting, and every map in the pool produces a payload
with no empty fields.

`--preview` skips the image check, so it shows the day's first candidate
rather than what the blank-paper check will settle on. It is the useful one
before publishing: it shows the next weeks of
maps without touching a file, which is the quickest way to judge whether the
filters are letting anything ugly through.

## What is in the pool, and what is not

`maps_harvest.py` walks eight LOC collections and drops anything that would
look bad or be legally awkward:

- `access_restricted` items, and anything not digitised as an image
- anything published after **1929** -- the pool is thousands of maps deep, so
  there is no reason to make a rights judgement on any single one
- scans under ~1100px on the short side or 2.5 megapixels: below that there
  is not enough ink left after the screen dithers it
- aspect ratios outside 0.55-3.6, which arrive as a stripe on a 800x480 panel
- titles that mean "one sheet of a set", "index", "photocopy", or Sanborn
  fire-insurance sheets, which are a fragment on screen with no context

What survives is scored (size, how close the shape is to the panel, whether
it has a description and a named maker, minus a penalty for hand-drawn
sketches on tracing linen) and the best 1200 per category are kept. The score
only decides what to keep when a category overflows; it does not bias which
map comes up on which day.

Today's pool is **4,452 maps**: cities 1169, military 1200, panoramas 1200,
railways 594, exploration 199, nature 90. Nature is the shallow one -- 90
maps is a three-month cycle before it repeats -- so it is the first category
to widen if the setting ships.

### The blank-paper check

Metadata cannot tell you whether a map is a dense engraved city view or four
streets sketched on a big sheet of paper, and the sparse ones look terrible
on a screen. The daily job settles it by measuring: it HEADs the fitted
greyscale JPEG and reads its size. At a fixed 800x480 that number is a direct
measure of how much ink is on the map -- hand-drawn plats come back at
16-29KB, engraved city views and railroad maps at 50-80KB. Anything under
`MIN_IMAGE_BYTES` (32KB) is passed over for the next map in the day's order.

### Tuning it

Everything above is a constant at the top of `harvest.py`:
`SOURCES`, `MAX_YEAR`, `MIN_SHORT_SIDE`, `MIN_ASPECT`/`MAX_ASPECT`,
`TITLE_REJECT`, `PER_CATEGORY_CAP`. Adding a collection is one line in
`SOURCES` plus a label in `CATEGORY_LABELS` in `daily.py`; the
daily job discovers the new category from the pool on its own and starts
writing a file for it.

Changing `SALT` in `daily.py` reshuffles the entire schedule.
Changing the pool changes future picks but not today's, because the pick is
recomputed from whatever the pool holds at the time.

## Failure behaviour

| What breaks | What happens |
|---|---|
| loc.gov is down or blocks the runner | harvest fails, pool stays as it is, screen unaffected |
| Harvest returns far fewer maps than the current pool | it refuses to write and exits non-zero, rather than shrinking the pool |
| The daily workflow fails or is skipped | yesterday's `map.json` stays committed and keeps rendering |
| A map's image 404s | the daily job HEAD-checks it and deterministically moves to the next candidate |
| A map turns out to be mostly blank paper | same skip, on the measured size of the fitted JPEG |
| tile.loc.gov is slow or 500s | the pick is *not* changed -- only a definitive 404/403/410 or a measured size skips a map |
| The pool file is missing or empty | the job exits non-zero without writing, leaving the last good files |

## Rights

The Library of Congress states that the content of the Geography and Map
Division's digitised collections is **free to use and reuse** unless an item
carries a Rights Advisory, with the credit line *"Library of Congress,
Geography and Map Division."* The harvest drops `access_restricted` items and
everything published after 1929, so the pool stays inside that statement
without needing a per-item judgement. Catalogue metadata is factual
description, reproduced here in short form.

Every payload carries `source`, `credit`, `rights` and `item_url`. Putting
the credit line somewhere on the screen -- a footer is enough -- keeps the
recipe publishable as-is.

## If it gets popular

`raw.githubusercontent.com` is fine for a handful of devices. If the recipe
is published and thousands of devices
start polling it, move the same files to GitHub Pages (same repo, a workflow
publishing the JSON, no rate limit worth worrying about) and change the
polling URL. Nothing else about the design has to change. A CDN that caches
by branch name (jsDelivr's `@main`) is the one thing to avoid, since it can
hold a stale `map.json` past midnight.

## API manners

The harvest runs once a month, sleeps two seconds between requests, backs off
on 429 and 5xx, identifies itself with a real user agent naming this repo,
and asks for 100 records per request instead of one record per request. A full
run is roughly 120 requests and takes about ten minutes. The daily job makes
at most a handful of HEAD requests to the image service -- and none at all to
the search API -- with a total time budget of four minutes.
