#!/usr/bin/env python3
"""
Fold a pasted decision block into curation.json.

Reads a comment body on stdin, finds the ```queue block, and merges it.
Everything about this is deliberately suspicious: it is the one path
where text from a comment box becomes what a plugin serves, so it
refuses anything it does not fully understand rather than doing its
best with it.

What it checks, and why each one:

  the block is for this queue      a postcard block must not land in the
                                   maps repo and silently veto by id
  every id exists in the pool      a typo, a truncated paste or a block
                                   from an older pool would otherwise
                                   write ids that match nothing, and the
                                   mistake would be invisible
  the window is not older than
  what has already been reviewed   pasting yesterday's block must not
                                   walk reviewed_to backwards and
                                   re-open a week already done

Decisions are a union with what is already there, never a replacement:
a block carries only the window it covered, and treating it as the whole
truth would drop every earlier decision.

Prints a one-line summary for the workflow to comment back. Exit 1 on a
refusal, with the reason on stderr.
"""
import json
import os
import re
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
CURATION = os.path.join(HERE, "curation.json")
POOL = os.path.join(HERE, "pool.json")
BLOCK = re.compile(r"```queue\s*\n(.*?)\n```", re.S)
KIND = "map-of-the-day"


def pool_ids():
    with open(POOL) as fh:
        pool = json.load(fh)
    entries = pool.get("maps") or pool.get("entries") or []
    return {str(e["id"]) for e in entries}


def fail(why):
    sys.stderr.write(why + "\n")
    return 1


def main():
    body = sys.stdin.read()
    found = BLOCK.search(body)
    if not found:
        return fail("no ```queue block in the comment")
    try:
        block = json.loads(found.group(1))
    except ValueError as e:
        return fail("the queue block is not valid JSON: {}".format(e))

    if block.get("queue") != KIND:
        return fail("that block is for {!r}, this repo is {!r}"
                    .format(block.get("queue"), KIND))

    try:
        with open(CURATION) as fh:
            curation = json.load(fh)
    except (OSError, ValueError):
        curation = {}

    known = pool_ids()
    vetoed = [str(i) for i in (block.get("vetoed") or [])]
    flags = block.get("untranslate") or []
    flag_ids = [str(f.get("id") if isinstance(f, dict) else f) for f in flags]

    unknown = sorted({i for i in vetoed + flag_ids if i not in known})
    if unknown:
        return fail("{} id(s) are not in the pool, e.g. {}; refusing the "
                    "whole block".format(len(unknown), ", ".join(unknown[:5])))

    was_to = curation.get("reviewed_to") or ""
    now_to = block.get("reviewed_to") or block.get("to") or ""
    if was_to and now_to and now_to < was_to:
        return fail("this block reviews to {} but {} is already reviewed; "
                    "refusing to go backwards".format(now_to, was_to))

    before_v = set(str(i) for i in (curation.get("vetoed") or []))
    before_f = set(str(i) for i in (curation.get("untranslate") or []))
    after_v = before_v | set(vetoed)
    after_f = before_f | set(flag_ids)

    curation["vetoed"] = sorted(after_v)
    curation["untranslate"] = sorted(after_f)
    if now_to:
        curation["reviewed_to"] = now_to
    with open(CURATION, "w") as fh:
        json.dump({"vetoed": curation["vetoed"],
                   "untranslate": curation["untranslate"],
                   "reviewed_to": curation.get("reviewed_to")},
                  fh, indent=1)
        fh.write("\n")

    # The flags are the part a person has to look at later, so they are
    # written out with what they need rather than as bare ids.
    notes = [f for f in flags if isinstance(f, dict) and f.get("source")]
    if notes:
        path = os.path.join(HERE, "flagged_text.json")
        try:
            with open(path) as fh:
                existing = json.load(fh)
        except (OSError, ValueError):
            existing = []
        seen = {n.get("id") for n in existing}
        existing.extend(n for n in notes if n.get("id") not in seen)
        with open(path, "w") as fh:
            json.dump(existing, fh, indent=1, sort_keys=True)
            fh.write("\n")

    print("{} vetoed (+{}), {} flagged (+{}), reviewed through {}".format(
        len(after_v), len(after_v - before_v),
        len(after_f), len(after_f - before_f),
        curation.get("reviewed_to") or "unchanged"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
