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
map.json                today's map across everything -- the simplest polling URL
today.json              today's map for every topic -- the feed with settings
sources.md              what was checked about the other image-of-the-day sources
.github/workflows/      the two jobs that run the above on a schedule
```

## How it works

Three moving parts, and only one of them touches the internet on a normal day:

| When | What runs | What it writes |
|---|---|---|
| Monthly | `harvest.py` (Refresh Map Pool workflow) | `pool.json` -- a few thousand vetted maps |
| Daily, 00:05 UTC | `daily.py` (Map of the Day workflow) | `map.json` and `today.json` |
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
3. Or, to offer topic settings, poll `today.json` instead -- same fields,
   one map per topic. See Settings below.
4. Write the markup against the fields below.
5. Save. The screen updates itself from then on.

The payload's keys arrive at the top of the template context, the same way
`launch.json` does for the mission control plugin -- `{{ title }}`,
`{{ image }}`, and so on.

## Settings

Settings split in two, and the split decides where each one lives.

**What the screen shows** is a markup decision. Every payload carries every
field, so showing or hiding the description is a Liquid conditional against a
TRMNL custom field -- no extra files, no job to re-run, effective the moment
you save:

```liquid
{% if show_description and description_short != "" %}
  <span class="description">{{ description_short }}</span>
{% endif %}
```

The fields that are **not** always present are the ones a conditional has to
guard: `creator` (95%), `description` and `description_short` (94%), `scale`
(~15%), `subjects` (99%). Everything else is on every map in the pool.

**Which map you get** is the harder one, because the feed is a static file
and the choice has to be made before the file is written. One file per
choice works for a single choice -- but someone who likes railways *and*
nautical charts wants a combination, and there are 2^n of those.

So `today.json` carries **today's map for every topic**, about 30KB, and the
markup picks. One URL, any combination, no server:

```liquid
{% assign pick = picks[topic] %}          {% comment %} one topic {% endcomment %}

{% comment %} several interests, rotating a day at a time {% endcomment %}
{% assign chosen = interests | split: "," %}
{% assign i = day_index | modulo: chosen.size %}
{% assign pick = picks[chosen[i]] %}

<h1>{{ pick.title_short }}</h1>
<img src="{{ pick.image }}">
```

`day_index` is the day number the whole schedule turns on, so the rotation is
stable for the whole day and moves on by itself at midnight UTC.

### The topics

Themes are matched against each map's title, subject headings and collection
-- deliberately not its description, which is catalogue prose and would tag
half the pool as nautical on the strength of a mention of a harbour. A map
carries as many themes as it matches, 1.45 on average, and every map matches
at least one. Eras are read off the year, so they cannot be wrong.

| Key | Shown as | Maps | Repeats after |
|---|---|---|---|
| `all` | All Maps | 4451 | 12 years |
| `city-plans` | City Plans | ~1380 | 3.8 years |
| `birds-eye-views` | Bird's-Eye Views | ~1220 | 3.4 years |
| `civil-war` | Civil War | ~715 | 2.0 years |
| `railroads` | Railroads | ~690 | 1.9 years |
| `roads-and-travel` | Roads & Travel | ~610 | 1.7 years |
| `revolution` | Revolutionary War | ~475 | 1.3 years |
| `land-ownership` | Land & Property | ~395 | 1.1 years |
| `battles-and-forts` | Battles & Forts | ~340 | 11 months |
| `nautical` | Nautical Charts | ~255 | 8 months |
| `exploration` | Exploration | ~230 | 7 months |
| `era-1700s` | The 1700s | ~700 | 1.9 years |
| `era-1800-1849` | 1800 - 1849 | ~360 | 1 year |
| `era-1850-1869` | 1850 - 1869 | ~1200 | 3.3 years |
| `era-1870-1899` | 1870 - 1899 | ~1650 | 4.5 years |
| `era-1900-1929` | 1900 - 1929 | ~485 | 1.3 years |

Exact sizes ride along in the feed, as `topics[].size` and each pick's
`topic_size`, so a settings panel can show them without hardcoding.

Two topics were considered and **left out** for repeating too fast to be
worth offering: national parks (99 maps, a quarterly loop) and world maps and
hemispheres (36, back round every five weeks). `--selftest` fails if any
offered topic drops under 200 maps, which is the line for "not twice in a
year".

Adding a topic is one line in `TOPIC_PATTERNS` in `harvest.py` plus one in
`THEMES` in `daily.py`, then a pool refresh. An era needs only the `ERAS`
line, since nothing has to be re-tagged.

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
| `description` | `Township and county map showing relief by hachures...` | trimmed to ~220 chars, **6% are empty** |
| `description_short` | `Shows ward numbers and boundaries.` | one sentence, <=120 chars, for a fixed-height caption |
| `medium` | `col. map 52 x 40 cm.` | the physical object |
| `published` | `New York, 1866` | imprint: where and when it was *published*, often not where it depicts |
| `scale` | `1:1,875,000` | **only ~15% of maps** carry one in their notes |
| `subjects` | `["Railroads", "Civil War, 1861-1865"]` | up to 4, specific terms only |
| `subjects_line` | `Railroads, Civil War, 1861-1865` | the same, pre-joined |
| `byline` | `New Hampshire. Railroad Commissioners - 1894` | creator and year, pre-joined |
| `subtitle` | `New Hampshire - Railroad Maps, 1828-1900` | place and collection, pre-joined |
| `imprint` | `New York, 1866 - 1:1,875,000` | publication and scale, pre-joined, either part may be missing |
| `image` | `.../full/!1872,1404/0/gray.jpg` | greyscale, sized for the largest panel |
| `image_og` | `.../full/!800,480/0/gray.jpg` | fitted to the OG's 800x480 |
| `image_x` | `.../full/!1872,1404/0/gray.jpg` | fitted to the larger panel's real pixels |
| `image_color` | `.../full/!1872,1404/0/default.jpg` | same size, original colour |
| `image_base` | `https://tile.loc.gov/image-services/iiif/service:gmd:...` | build any other size from it -- see below |
| `thumb` | `.../full/!320,320/0/gray.jpg` | for a mashup layout |
| `image_width`, `image_height` | `4784`, `6488` | the scan's native size |
| `aspect`, `orientation` | `0.737`, `portrait` | pick a layout without measuring |
| `source`, `credit`, `rights` | `Library of Congress, Geography and Map Division` | attribution line |
| `item_url` | `https://www.loc.gov/item/98688514/` | the record, for a QR code or footer |
| `date`, `category`, `category_label` | `2026-09-08`, `railroads`, `Railroads` | which topic this pick answers |
| `day_index`, `topic_size` | `20704`, `693` | the rotation number, and how many maps the topic holds |
| `pool_size`, `pool_generated`, `generated` | | diagnostics |
| `image_checked`, `ink_bytes` | `ok`, `37276` | what the daily check found, and the ink measurement below |

### Sizes, and why there is no single right one

Images come from the Library's IIIF service, where a size is just part of the
URL. `!w,h` means "fit inside this box without distorting" and `gray.jpg`
asks the Library's server for the greyscale conversion, so the picture
arrives already the right shape and colour space for a panel to dither.

TRMNL's panels are not one size. The framework gives the OG an 800x480
viewport and the larger, 4-bit panel a 1040x780 one -- but that second
number is the CSS box, and the panel behind it is **1872x1404** physical
pixels at 227 ppi. An image sized to the box renders soft on it.

So the payload does not commit to a panel:

- `image` is the largest of the known sizes, which is the safe default. It
  is fetched by whatever renders your markup, not by the battery-powered
  screen, so the extra pixels cost the device nothing and let the renderer
  downsample -- which looks better than upscaling ever does.
- `image_og` and `image_x` are the two panels, if you would rather be exact.
- `image_base` is the escape hatch. Any size at all is
  `{{ image_base }}/full/!<w>,<h>/0/gray.jpg` -- `!1040,780` for the CSS box,
  `!1404,1872` for a portrait layout, `!2400,2400` for a zoomed detail. Ask
  for more than the scan holds and the Library upscales rather than refusing,
  so a template can be written without checking each map's resolution.

The pool keeps only scans of at least 1400px on the short side, so the larger
panel is fed real pixels rather than an enlargement. `image_width` and
`image_height` are the scan's true size if you want to decide in markup.

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
- scans under 1400px on the short side or 2.5 megapixels: the larger panel is
  1404px on its short side, so anything under that is being enlarged before
  it is even dithered
- aspect ratios outside 0.55-3.6, which arrive as a stripe on a landscape
  panel; scoring prefers the 1.33-1.67 band the panels themselves span
- titles that mean "one sheet of a set", "index", "photocopy", or Sanborn
  fire-insurance sheets, which are a fragment on screen with no context

What survives is scored (size, how close the shape is to the panel, whether
it has a description and a named maker, minus a penalty for hand-drawn
sketches on tracing linen) and the best 1200 per category are kept. The score
only decides what to keep when a category overflows; it does not bias which
map comes up on which day.

Today's pool is **4,451 maps**: cities 1168, military 1200, panoramas 1200,
railways 594, exploration 199, nature 90. Nature is the shallow one -- 90
maps is a three-month cycle before it repeats -- so it is the first category
to widen if the setting ships.

### The blank-paper check

Metadata cannot tell you whether a map is a dense engraved city view or four
streets sketched on a big sheet of paper, and the sparse ones look terrible
on a screen. The daily job settles it by measuring: it HEADs the map at a
fixed 800x480 and reads the JPEG's size. Held at one size, that number is a
direct measure of how much ink is on the map -- hand-drawn plats come back at
16-29KB, engraved city views and railroad maps at 50-80KB. Anything under
`MIN_INK_BYTES` (32KB) is passed over for the next map in the day's order,
and the reading is reported as `ink_bytes`.

The 800x480 there is a yardstick, not a display size: measuring every map at
the same size is what makes the threshold mean the same thing for all of
them. It has nothing to do with which panel ends up showing the map.

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
