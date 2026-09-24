#!/usr/bin/env python3
"""
Put the captions into English.

A good share of the pool is catalogued in whatever language is printed on
the sheet, which for old maps means Latin, Dutch, French, German, Spanish
and more. Faithful to the object, no use at all to somebody who cannot
read it, and a panel has room for one caption rather than two.

So the English replaces the original rather than sitting beside it. The
original stays in the pool either way, because a machine translation is
not the record and should never be mistaken for it -- it is a caption.

Translation is offline and free: Argos Translate, no key, no service to
depend on, language packs a couple of megabytes each. Roughly a second
per title, so the work is budgeted per run and cached forever in
translations.json, keyed by the *title* rather than by the card. That
last detail matters more than it sounds: a title shared by ten sheets of
one atlas is translated once.

Applied when daily.py loads the pool, not when harvest.py writes it, so
a better translation never needs a re-crawl.

    python3 translate.py                 # a budget of titles
    python3 translate.py --budget 4000
    python3 translate.py --report
"""

import argparse
import collections
import json
import re
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
POOL_PATH = os.path.join(HERE, "pool.json")
CACHE_PATH = os.path.join(HERE, "translations.json")

BUDGET = 1200
MIN_CONFIDENCE = 0.55

# A caption is usually "PLACE. What you are looking at", and the place is
# the whole point of a postcard. Left to itself the translator treats it
# as vocabulary: on the postcard side "Dameron. Le coin
# des laveuses" came back as "Lady. The corner of the washing machines". So the head is held back and only the
# description is translated.
HEAD_SPLIT = re.compile(r"^(.{2,40}?)((?:\.\s+|\s+[-–]\s+))(.+)$")

# Detection on a five-word caption is a coin toss between neighbouring
# languages -- "Constantinople. Obelisque de Theodose" came back as
# Portuguese, which turned Constantinople into Constantine. Where the
# source is single-language we say so instead of guessing.
SOURCE_LANG = {}          # one source, many languages: no hint to give

NON_LATIN = re.compile(r"[^\x00-\x7F\u00C0-\u024F\u1E00-\u1EFF]")

# Detected on short place-name captions, these are almost always a
# misread of a neighbouring language, and Argos has no pack for most of
# them anyway. Skipping is better than translating from the wrong one.
SKIP_LANGS = {"en", "af", "no", "da", "sw", "tl", "cy", "so", "et"}


# ============================================================
# what a translation is not allowed to do
# ============================================================

# A caption of one word is a name -- a place, a battlefield, a country --
# and a translator handed a name with no sentence around it translates it
# as vocabulary. In this cache Antietam came back "Antimony", Atlanta and
# Berkeley both "Home", Maine "Repute", Austria-Hungary "Austria-Hunger".
# Venezia to Venice is the only real gain among them, and it is not worth
# the trade: a German noun left in German is a caption a reader
# half-follows, a battlefield renamed "Antimony" is one that lies.
def is_one_word(title):
    return len(title.split()) == 1


# And nothing may appear in a translation that was not in its source.
# Machine translation of a short contextless string occasionally invents
# rather than errs -- on the postcards side a Turkish town came back as
# an obscenity and a Romanian caption as a racial slur. Comparison, not a
# word list over the output, so a real place name in the source survives.
SLURS = re.compile(
    r"\b(fuck\w*|shit\w*|cunt\w*|bitch\w*|bastard|wank\w*|arse\w*|asshole|"
    r"nigg\w+|fag(?:got)?s?|whore|slut|piss\w*|dick(?:head)?|prick|"
    r"chink|spic|kike|wetback|retard\w*|tranny)\b", re.I)


def invents_slur(source, english):
    if not english:
        return False
    found = {m.group(0).lower() for m in SLURS.finditer(english)}
    if not found:
        return False
    already = {m.group(0).lower() for m in SLURS.finditer(source or "")}
    return bool(found - already)


def usable(source, english):
    """Whether a translation may be published at all."""
    if not english:
        return False
    if is_one_word(source):
        return False
    return not invents_slur(source, english)


def upcoming_titles(pool, days):
    """
    The titles that will actually be on a screen in the next `days`,
    soonest first.

    The schedule is arithmetic, so this is knowable rather than
    guessable -- and it is the difference between a translation pass
    that shows up tomorrow and one that shows up whenever it reaches
    that sheet. For each topic the order within a cycle is fixed, so it
    is computed once and walked rather than asked for day by day.
    """
    sys.path.insert(0, HERE)
    import daily
    from datetime import date

    entries = pool["maps"]
    today = date.today()
    themes = [slug for slug, _ in daily.THEMES]
    eras = [slug for slug, _, _, _ in daily.ERAS]
    topics = themes + eras + [daily.cell_key(t, e)
                              for t in themes if t != "all" for e in eras]

    seen, ordered = set(), []
    for topic in topics:
        subset = daily.maps_for(entries, topic)
        if not subset:
            continue
        total = len(subset)
        cycle, position = divmod(daily.day_index(today), total)
        run = daily.order_for(subset, topic, cycle)
        for step in range(min(days, total)):
            index = position + step
            entry = (run[index] if index < total else
                     daily.order_for(subset, topic, cycle + 1)[index - total])
            if entry["t"] not in seen:
                seen.add(entry["t"])
                ordered.append((step, entry["t"]))
    ordered.sort(key=lambda row: row[0])
    return [title for _, title in ordered]


def load_cache():
    try:
        with open(CACHE_PATH) as fh:
            return json.load(fh).get("titles") or {}
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"version": 1, "count": len(cache), "titles": cache},
                  fh, ensure_ascii=False, separators=(",", ":"),
                  sort_keys=True)
        fh.write("\n")
    os.replace(tmp, CACHE_PATH)


def split_head(title):
    """(place, separator, rest) if the caption opens with a place."""
    m = HEAD_SPLIT.match(title)
    if not m:
        return None, "", title
    head, sep, rest = m.groups()
    # A head with a verb in it is a sentence, not a place name.
    if len(head.split()) > 5:
        return None, "", title
    # Only hold back a head the reader could already read. Protecting a
    # Greek or Cyrillic one leaves the caption unreadable, which is the
    # thing this whole exercise is for: "Άνατολικὴ ἄποψις ... - Άθῆναι"
    # came back with its head intact and its point lost.
    if NON_LATIN.search(head):
        return None, "", title
    return head, sep, rest


def detect(text):
    from langdetect import detect_langs, DetectorFactory, LangDetectException
    DetectorFactory.seed = 0
    try:
        best = detect_langs(text)[0]
    except LangDetectException:
        return None
    return best.lang if best.prob >= MIN_CONFIDENCE else None


def installed_pairs():
    import argostranslate.translate as t
    return {(a.code, b.code) for a in t.get_installed_languages()
            for b in a.translations_to if hasattr(a, "translations_to")} \
        if False else {
            (a.code, b.code)
            for a in t.get_installed_languages()
            for b in t.get_installed_languages()
            if a.code != b.code and a.get_translation(b)}


def ensure_pack(code, available, installed):
    """Install a language pack on demand, once."""
    if (code, "en") in installed:
        return True
    package = next((p for p in available
                    if p.from_code == code and p.to_code == "en"), None)
    if package is None:
        return False
    sys.stderr.write(f"  installing {code}->en\n")
    package.install()
    installed.add((code, "en"))
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--budget", type=int, default=BUDGET)
    ap.add_argument("--upcoming", type=int, default=45, metavar="DAYS",
                    help="translate what is due in the next N days first")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    with open(POOL_PATH) as fh:
        pool = json.load(fh)
    entries = pool["maps"]
    cache = load_cache()

    if args.report:
        done = sum(1 for e in entries if e["t"] in cache)
        langs = collections.Counter(v.get("lang") for v in cache.values())
        print(f"{len(cache)} titles translated, covering {done} of "
              f"{len(entries)} cards")
        for code, n in langs.most_common(12):
            print(f"  {code}  {n}")
        return 0

    import argostranslate.package as package
    import argostranslate.translate as translate

    package.update_package_index()
    available = package.get_available_packages()
    installed = installed_pairs()

    # What is due soon goes first, then everything else.
    queue = upcoming_titles(pool, args.upcoming) if args.upcoming else []
    rest = [e["t"] for e in entries]
    titles = [t for t in dict.fromkeys(queue + rest) if t not in cache]
    if queue:
        due = len([t for t in dict.fromkeys(queue) if t not in cache])
        sys.stderr.write(f"{due} of them are on a screen within "
                         f"{args.upcoming} days\n")
    # Where a source speaks one language, its word beats a detector's.
    hints = {}
    for entry in entries:
        code = SOURCE_LANG.get(entry.get("src"))
        if code:
            hints.setdefault(entry["t"], code)
    sys.stderr.write(f"{len(titles)} untranslated titles, budget "
                     f"{args.budget}\n")

    done = skipped = 0
    started = time.monotonic()
    for title in titles:
        if done >= args.budget:
            break
        code = hints.get(title) or detect(title)
        if code is None or code in SKIP_LANGS:
            cache[title] = {"lang": code or "??", "en": None}
            skipped += 1
            continue
        if not ensure_pack(code, available, installed):
            cache[title] = {"lang": code, "en": None}
            skipped += 1
            continue
        head, sep, rest = split_head(title)
        try:
            english = translate.translate(rest, code, "en").strip()
        except Exception:
            continue
        if head:
            english = head + sep + english
        # A translation identical to the original is a proper name that
        # came through untouched, which is the right answer and not worth
        # a second copy.
        cache[title] = {"lang": code,
                        "en": english if english and english != title else None}
        done += 1
        if done % 100 == 0:
            save_cache(cache)
            rate = done / max(time.monotonic() - started, 1)
            sys.stderr.write(f"    {done}/{min(args.budget, len(titles))} "
                             f"({rate:.1f}/s)\n")
            sys.stderr.flush()
    save_cache(cache)
    print(f"translated {done}, skipped {skipped}, cache now {len(cache)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# What a translation must never do, at the strings that taught each rule.
USABLE_CASES = [
    # a one-word caption is a name, and a name is not vocabulary
    (False, "Antietam", "Antimony"),
    (False, "Atlanta", "Home"),
    (False, "Maine", "Repute"),
    (False, "Graubunden", "Grey bandages"),
    (False, "Alt-Graz", "Old Great"),
    # including the ones it got right, which is the trade being made
    (False, "Venezia", "Venice"),
    (False, "Glockenturm", "Bell Tower"),
    # nothing foul the source did not say
    (False, "Selcuk", "Fuck."),
    (False, "Bereg Baikala", "Fuck that time."),
    (False, "Tigani ciurari", "Tiger niggers"),
    # but a real place name survives, because the source says it too
    (True, "Bitche (Lorraine), Le camp", "Bitche (Lorraine), The camp"),
    (True, "Camp de Bitche (Lorraine", "Bitche Camp (Lorraine)"),
    # and ordinary captions are untouched
    (True, "Alt Graz", "Old Graz"),
    (True, "Beleuchteter Uhrturm", "Illuminated Clock Tower"),
    (True, "Arnhem, Rijnbrug", "Arnhem, Rhine Bridge"),
    # nothing to publish is not publishable
    (False, "Graz", ""),
]


def usable_failures():
    """Empty when every guard case holds."""
    return ["usable({!r}, {!r}) = {}, want {}".format(s, e, usable(s, e), w)
            for w, s, e in USABLE_CASES if usable(s, e) is not w]
