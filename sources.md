# Image-of-the-day sources: what is worth building next

Notes from evaluating candidate sources for the "one striking picture a day"
family of recipes. Everything marked *verified* was requested from this
environment on 2026-09-08; everything else is marked as unverified so nobody
builds on a guess.

## The pattern all of these share

The Map of the Day implementation is deliberately generic:

```
harvest (monthly, GitHub Actions)  ->  pool.json   (vetted candidates)
select  (daily,   GitHub Actions)  ->  today.json  (one item, deterministic)
TRMNL   (every refresh)            ->  static file + image URL
```

A second recipe is a second harvester writing the same pool shape
(`{id, title, year, creator, place, description, image service, w, h}`) and a
copy of `maps_daily.py` with different labels. The daily selection, the
same-map-all-day guarantee, the offline-by-default failure behaviour and the
"no user infrastructure" property all come along for free. Judge a new source
mainly on whether it can fill that pool.

## What makes an image work on 1-bit e-ink

Worth stating, because it reorders the candidate list: **line art beats
photography**. Engravings, technical drawings, bird's-eye views, botanical
plates, patent figures and hand-drawn maps survive dithering; a dark
oil painting or a nebula photograph turns to mud. Sources are ranked below
with that weighting, not by how famous the collection is.

## Verified today

| Source | Key? | Status | Notes |
|---|---|---|---|
| LOC maps (`loc.gov/collections/...?fo=json`) | none | **200, verified** | Also verified IIIF `!w,h` fitting, `gray.jpg` quality, and `info.json`. Cloudflare challenge does not appear from a GitHub runner or from here -- it is the TRMNL polling environment that gets challenged, which is why the fetch moved to Actions. |
| Met Museum (`collectionapi.metmuseum.org`) | **none** | **200, verified** | Returns `isPublicDomain`, `primaryImage`, `primaryImageSmall`, `artistDisplayName`, `objectDate`. |
| Cleveland Museum of Art (`openaccess-api.clevelandart.org`) | **none** | **200, verified** | Supports `cc0=1&has_image=1` filters directly in the query. |
| Biodiversity Heritage Library (`biodiversitylibrary.org/api3`) | required | **401 without a key** | Key is free and self-service; it would live in an Actions secret, never in a user's plugin. |
| Smithsonian Open Access (`api.si.edu/openaccess`) | required | **403 without a key** | Same: free api.data.gov key, held by the recipe author. |

## Candidate ranking

### 1. Map of the Day -- built

Large pool (thousands after filtering), no key, IIIF resizing and greyscale
done server-side, rights covered by a division-wide statement, and the
content is line art. See `docs/daily-map.md`.

### 2. Object of the Day, from the Met -- the obvious next one

The only large art collection found with **no API key at all** and an
explicit `isPublicDomain` flag per object. `/objects` returns every object
id; `/search?q=&hasImages=true&departmentId=` narrows by department, which
is the natural category setting (Arms and Armour, Asian Art, Drawings and
Prints, Musical Instruments...).

- **Pool**: ~490k objects, ~400k with images, a large share public domain.
- **Rights**: objects flagged `isPublicDomain: true` are published by the Met
  under CC0. Filter on the flag and the rights question is closed.
- **e-ink**: mixed. Paintings are risky, but *Drawings and Prints*, *Arms and
  Armour*, *Musical Instruments* and Japanese woodblock prints are excellent.
  Restricting the departments is what makes this recipe good rather than
  average.
- **Work**: harvesting needs one request per object (no bulk metadata
  endpoint), so build the pool from department searches and page through it
  slowly over a monthly run, or seed from the Met's public CSV dump.
- **Watch out for**: no IIIF resizer, so the payload has to point at
  `primaryImageSmall`; and object images vary wildly in framing (some are
  gallery photographs with grey backgrounds).

### 3. Scientific Illustration of the Day, from BHL -- the best content, the most work

The content is the strongest fit for the screen of anything surveyed: 19th
century engraved botanical and zoological plates are pure line work.

- **Pool**: very large, tens of thousands of illustration pages.
- **Key**: free, self-service, held by the recipe author in an Actions secret.
  Users never see it. (Unverified beyond the 401 above -- get a key and probe
  `GetItemMetadata` with `pages=t` first.)
- **The hard part**: finding the *illustrated* pages. BHL page metadata
  carries page types (Text, Title Page, Illustration, Table of Contents),
  which is the field to filter on; if coverage turns out to be patchy, the
  fallback is BHL's curated Flickr stream (another key) or seeding from
  hand-picked volumes -- a hundred good volumes is already years of daily
  plates.
- **Rights**: mixed per item (public domain, CC-BY, some NC). Unlike LOC there
  is no blanket statement, so the harvester must read and filter the per-item
  rights field, and drop anything that is not clearly permissive.

### 4. Smithsonian Object of the Day -- broad, CC0, needs a key

Millions of records, a large explicitly-CC0 subset, structured metadata, and
`online_media_type:Images` plus `usage:CC0` are queryable filters. Costs a
free api.data.gov key and overlaps heavily with the Met concept; worth doing
as a *different* recipe only if it is narrowed to something the Met cannot
give -- natural history specimens, or the Air and Space collection.

### 5. Patent drawing of the day -- great idea, awkward pipeline

Patent figures are the single best-suited images for 1-bit rendering, and the
concept is genuinely funny and shareable. The problem is purely mechanical:
the modern USPTO search APIs return bibliographic data, and the drawings are
page images inside multi-page TIFFs behind a separate viewer. PatentsView
needs a key and carries no drawings at all.

Realistic routes, in order of promise: harvest a curated set of expired
patents and extract figure pages once, committing the images to the repo
alongside the metadata; or use Google Patents'
static PDF/PNG hosting, which is undocumented and could vanish. Not a
starting point, but the best candidate for a hand-curated pool of a few
hundred items, which needs no API at all after the initial pass.

### 6. Everything else considered

| Concept | Verdict |
|---|---|
| NASA APOD | Free key, one image a day already chosen for you -- but half are colour nebula photographs that dither badly, and some entries are videos with no image at all. Good as a *fallback* source, weak as the whole recipe. |
| Wikimedia Commons picture of the day | No key, but the selection skews to modern colour photography and the daily choice is not yours to filter. |
| Rijksmuseum | Key required; superb print and drawing collection; strong second choice after the Met if a European flavour is wanted. |
| NYPL Digital Collections | Key required; excellent oddities (menus, cigarette cards, city photographs); rights vary per item. |
| Internet Archive | No key, enormous, but no reliable way to tell an interesting plate from a scanned page of text. Selection quality is the whole problem and it does not solve it. |
| Historical advertisements / posters | Content is ideal for the screen. No single good API; would come from LOC's poster collections, which is the same harvester with a different `SOURCES` list -- probably the cheapest second recipe of all. |

## Recommended order

1. **Ship Map of the Day.** It is built, keyless and legally clean.
2. **Poster / advertisement of the day**, if a quick second recipe is wanted:
   same code, different LOC collections, roughly an afternoon.
3. **Met Object of the Day**, for a recipe that is not a Library of Congress
   recipe: no key, CC0 flag, needs department curation to look good.
4. **BHL Scientific Illustration**, once someone has a key and has confirmed
   the page-type filter works. Best content, most uncertainty.
