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
import gzip
import json
import re
import os
import sys
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
POOL_PATH = os.path.join(HERE, "pool.json")
CACHE_PATH = os.path.join(HERE, "translations.json")

BUDGET = 1200
MIN_CONFIDENCE = 0.55

# How much of a caption may come through untouched before the translation
# is not worth publishing, and the length below which the measure says
# nothing. Both settled by measuring the cache.
BARELY_SHARE = 0.75
BARELY_FLOOR = 6
WORDS = re.compile(r"[^\W\d_]{2,}", re.U)

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
    if invents_slur(source, english):
        return False
    if loses_content(source, english):
        return False
    if only_recased(source, english):
        return False
    if mangles_numbers(source, english):
        return False
    if loses_a_numeral(source, english):
        return False
    if barely_changed(source, english):
        return False
    if invents_a_word(source, english):
        return False
    return True


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
    # a translation that hands back most of the caption untouched has
    # translated nothing, and says otherwise
    (False, "Paris (17e), la rue Demours, Le Restaurant du Grand Veneur",
     "Paris (17th), rue Demours, Le Restaurant du Grand Veneur"),
    (False, "Rabat - Musee des Arts indigenes des Oudaias",
     "Rabat - Musee des Arts Indigenouss des Oudaias"),
    # but a short caption is allowed its one honest change
    (True, "Bitche (Lorraine), Le camp", "Bitche (Lorraine), The camp"),
    # a date must survive, in either script
    (False, "Monreale. Abside della Cattedrale (XII secolo",
     "Monreale. Cathedral apse (16th century)"),
    (True, "Monreale. Abside della Cattedrale (XII secolo",
     "Monreale. Cathedral apse (12th century)"),
    (True, "Charte uber die XIII Vereinigte Staaten von Nord-America",
     "Chart of the XIII United States of North America"),
    # a word that exists in no language, which is what a translator does
    # with a word it cannot translate
    (False, "Rabat - Musee des Arts indigenes des Oudaias",
     "Rabat - Musee des Arts Indigenouss des Oudaias"),
    (False, "Nafplio, Peloponnese", "Napple, Peloponnese"),
    (False, "Attaques des retranchemens devant le Fort Carillon",
     "Attacks of the entrechemens in front of Fort Carillon"),
    # but a plain translation of ordinary words is not an invention
    (True, "Vier verschiedene Ansichten von Graz",
     "Four different views of Graz"),
    (True, "Aufnahmen im Gebirgslande westlich von Peking",
     "Recordings in the mountains west of Beijing"),
    # and a transliteration cannot be looked up, so it is not judged here
    (True, "\u0391\u03b8\u03b7\u03bd\u03b1\u03b9 - \u03a0\u03c1\u03bf\u03c0\u03cd\u03bb\u03b1\u03b9\u03b1 \u0386\u03ba\u03c1\u03bf\u03c0\u03cc\u03bb\u03b5\u03c9\u03c2",
     "Athens - Acropolis Propylaia"),
    # nothing to publish is not publishable
    (False, "Graz", ""),
]


def usable_failures():
    """Empty when every guard case holds."""
    return ["usable({!r}, {!r}) = {}, want {}".format(s, e, usable(s, e), w)
            for w, s, e in USABLE_CASES if usable(s, e) is not w]


# ------------------------------------------------------------
# What a diagnostic pass over the cache turned up
# ------------------------------------------------------------
# Four failure classes, each with its own right answer. None of them was
# reported: they came out of comparing every translation with its source
# after a single caption was flagged, which is the argument for looking
# at flags rather than only acting on them.

def loses_content(source, english):
    """
    Half the words gone is not a translation.

    "Boschi DI Cocchi A Genale" came back as "Woodworking"; "Baikal.
    Skala Malaia Kolokolnia" as "Baikal. scale". The translator reached
    the end of what it could parse and stopped, and the caption that
    survives describes a different thing.
    """
    src, out = source.split(), english.split()
    return len(src) >= 4 and len(out) <= len(src) / 2


def only_recased(source, english):
    """
    Identical but for capitals: the detector was wrong and there was
    nothing to translate. "A Cactus garden" to "A Cactus Garden" costs a
    cache entry and gains a reader nothing.
    """
    return source.strip().lower() == english.strip().lower()


def mangles_numbers(source, english):
    """
    "1RE Compagnie DU 1ER North Nigerien" came back as "1st 1st Company
    of the 1st North Nigerian" -- a numeral duplicated across the line.
    Dates, regiments and street numbers are the part of a caption a
    reader is most likely to take at face value.
    """
    want = re.findall(r"\d+", source)
    got = re.findall(r"\d+", english)
    if want == got:
        return False
    # A date written the old way may legitimately come back in Arabic
    # numerals -- "(XII secolo" to "12th century" is the right answer, and
    # comparing digit lists alone would refuse it for gaining a number
    # the source never wrote in digits. Only the value the source
    # actually states is forgiven; loses_a_numeral has the rest.
    forgiven = {str(value) for _, value in roman_numerals(source)}
    return want != [g for g in got if g not in forgiven]


# ------------------------------------------------------------
# What the first flagged captions turned up
# ------------------------------------------------------------
# Two more classes, both found by reading the three captions a person
# flagged and then measuring the whole cache for the same fault. Neither
# was rare.

def barely_changed(source, english):
    """
    A caption that comes back still in its own language, minus a word.

    "Paris (17e), la rue Demours, Le Restaurant du Grand Veneur" was
    published as "Paris (17th), rue Demours, Le Restaurant du Grand
    Veneur": the arrondissement turned into English, an article dropped,
    and the rest left exactly as it was. "Rabat - Musee des Arts
    indigenes des Oudaias" kept six of its seven words and mangled the
    seventh into "Indigenouss".

    Measured across the cache, a translation that hands back
    three-quarters of the source untouched is worth nothing: half of
    them still read as another language outright, and almost all the
    rest only moved a comma, doubled a year ("Amherst, Mass. 1886 1886")
    or broke a diacritic ("Culhuacan" to "Culhuaca n"). Not one was a
    caption a reader was better off for.

    A short caption is exempt, because at four words a single honest
    change already leaves three-quarters standing -- "Bitche (Lorraine),
    Le camp" to "Bitche (Lorraine), The camp" is a real translation and
    must survive. Six words is where the measure starts meaning
    something.
    """
    src = [w.lower() for w in WORDS.findall(source)]
    if len(src) < BARELY_FLOOR:
        return False
    left = collections.Counter(w.lower() for w in WORDS.findall(english))
    survived = 0
    for word in src:
        if left[word]:
            left[word] -= 1
            survived += 1
    return survived >= BARELY_SHARE * len(src)


# A Roman numeral as a cartouche writes it, dots and all: MDCCLXV, XII,
# M.DCC.LXV. Three letters at least, because "DI" and "IL" are Italian
# words, "VI" is both, and "M.V." is somebody's initials -- a caption
# gains nothing from arguing about them.
ROMAN = re.compile(r"(?<![A-Za-z.])((?:[MDCLXVI]+\.?){1,6})(?![A-Za-z])")
ROMAN_VALUE = {"M": 1000, "D": 500, "C": 100, "L": 50, "X": 10, "V": 5, "I": 1}


def roman_numerals(text):
    """Every Roman numeral in the text, with what it is worth."""
    found = []
    for match in ROMAN.finditer(text):
        letters = match.group(1).replace(".", "").upper()
        if len(letters) < 3 or not all(c in ROMAN_VALUE for c in letters):
            continue
        total = previous = 0
        for letter in reversed(letters):
            value = ROMAN_VALUE[letter]
            total += -value if value < previous else value
            previous = max(previous, value)
        found.append((match.group(1), total))
    return found


def loses_a_numeral(source, english):
    """
    A year or a century that did not survive being translated.

    mangles_numbers watches digits, so a date written the old way walks
    straight past it. "M.DCC.LXV" came back as "Mr.DCC.LXV" -- the M read
    as an abbreviation for a man. "(XII secolo" became "16th century",
    which is not a clumsy caption but a wrong one, and a reader has no
    way to know. "LXXX A Esposizione Nazionale" lost its eightieth
    entirely and gained a musical note.

    Either script counts: "XII secolo" to "12th century" is exactly
    right, and so is leaving the numeral alone.
    """
    lowered = english.lower()
    for token, value in roman_numerals(source):
        if token.lower() in lowered:
            continue
        if re.search(r"(?<!\d){}(?!\d)".format(value), english):
            continue
        return True
    return False


# ------------------------------------------------------------
# Words the translator made up
# ------------------------------------------------------------
# The third class the flagged captions turned up, and the one that needed
# a dictionary. The corpus cannot serve as one: a vocabulary built from
# every English caption in both archives is 16,000 words and has no
# "roofs", no "conquer" and no "eternal", so real English reads as
# invented and the test flags a third of everything. words_en.txt.gz is
# 370,105 words, and it separates the two cleanly -- it has "roofs" and
# "potassium", and it has no "Indigenouss", "deboutmen" or "corrigerated".

WORDS_PATH = os.path.join(HERE, "words_en.txt.gz")

# Apostrophes stay inside a word, so "Qur'an" is one word rather than a
# fragment called "Qur".
TOKEN = re.compile(r"[^\W\d_]+(?:['\u2019][^\W\d_]+)*", re.U)

# Anything outside Latin and its extensions. A caption in Greek, Cyrillic
# or Japanese has to be transliterated, and no dictionary can tell a good
# transliteration from a bad one -- "Propylaia" and "Zappeion" are exactly
# right and are in no wordlist. Those captions are left to the other
# guards.
FOREIGN_SCRIPT = re.compile(r"[^\u0000-\u024f\u1e00-\u1eff]")

_known = None


def flatten(text):
    """One spelling to compare: lower case, no accents, straight quotes."""
    decomposed = unicodedata.normalize(
        "NFD", unicodedata.normalize("NFC", text).lower())
    return "".join(c for c in decomposed
                   if not unicodedata.combining(c)).replace("\u2019", "'")


def known_words():
    """Every word that is not evidence of anything, loaded once.

    Two sources. The wordlist is English. The corpus is everything these
    archives already say in any language -- place names, collections,
    cataloguing jargon, the captions themselves -- because a translator
    copying "Beijing" or "Graz" through has invented nothing, and neither
    list alone covers both.
    """
    global _known
    if _known is not None:
        return _known
    words = set()
    try:
        with gzip.open(WORDS_PATH, "rt", encoding="utf-8") as fh:
            words.update(flatten(line.strip()) for line in fh if line.strip())
    except OSError:
        # No list, no guard. Refusing every translation because a data
        # file is missing would be worse than publishing them.
        _known = set()
        return _known
    try:
        with open(POOL_PATH) as fh:
            pool = json.load(fh)
        for entry in (pool.get("maps") or pool.get("entries") or []):
            for value in entry.values():
                for text in (value if isinstance(value, list) else [value]):
                    if isinstance(text, str):
                        words.update(flatten(t) for t in TOKEN.findall(text))
    except (OSError, ValueError):
        pass
    _known = words
    return _known


def is_known(word):
    """Whether this is a word somebody uses, in any of these languages."""
    known = known_words()
    if not known:
        return True
    flat = flatten(word)
    for form in (flat, flat[:-2] if flat.endswith("'s") else flat,
                 flat.replace("'", "")):
        if form in known:
            return True
    # Archaic English the wordlist does not carry: beareth, goeth.
    for suffix in ("eth", "est"):
        if flat.endswith(suffix) and (flat[:-len(suffix)] in known
                                      or flat[:-len(suffix)] + "e" in known):
            return True
    return False


def invents_a_word(source, english):
    """
    A word in the translation that exists in no language.

    The translator, handed a word it cannot translate, sometimes half
    copies it and produces something that is a word in nothing:
    "indigenes" came back as "Indigenouss", "regionis" as "Regionss",
    "debouquemens" as "deboutmen", "retranchemens" as "entrechemens".
    Worse, it does this to names a reader would look up -- Nafplio
    published as "Napple", Perkasie as "Perkassie", Soest as "Soust",
    Seinenkan as "Seisenkan".

    Judged by hand over a sample, nearly every map caught this way is
    genuinely mangled; about one postcard in six is a decent translation
    caught by a gap in the wordlist ("Surinamese", "winegrowers"). That
    trade is worth making, because the two mistakes do not cost the same:
    a translation wrongly refused falls back to the archive's own words,
    which a reader can still read, while one wrongly published puts a
    place name on a wall that does not exist.
    """
    if FOREIGN_SCRIPT.search(source):
        return False
    said = {flatten(t) for t in TOKEN.findall(source)}
    for word in TOKEN.findall(english):
        flat = flatten(word)
        if len(flat) <= 2 or flat in said or is_known(word):
            continue
        # A word trimmed rather than invented: "Skeppsholmsbron" to
        # "Skeppsholm", "Obshchii" to "Obshchi".
        if any(s.startswith(flat) and len(flat) >= 5 for s in said):
            continue
        return True
    return False


TRAILING_JUNK = re.compile(r"[\u2018\u2019'\"]+$")


def tidy(source, english):
    """
    Strip a trailing quote mark the translator added. "Bismarckplatz"
    came back as "Bismarckplatz'". Repaired rather than refused: the
    rest of the line is a good translation and only the last character
    is wrong.
    """
    if not english:
        return english
    if TRAILING_JUNK.search(english) and not TRAILING_JUNK.search(source or ""):
        return TRAILING_JUNK.sub("", english).rstrip()
    return english
