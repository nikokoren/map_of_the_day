#!/usr/bin/env python3
"""
Which flagged captions are still waiting for a replacement.

Flagging a caption is only half a repair. It drops a bad translation, so
the archive's own words come back -- but where there was never a
translation, a flag changes nothing a reader sees. The other half is
somebody reading the source, working out what went wrong, and writing
the line that should have been there, into corrections.json.

Nothing connected those halves. flagged_text.json was written and never
read, so flags accumulated in a file with no reader while the captions
they named went on being wrong. This is the reader.

  python3 pending_rewrites.py            what is waiting, for a person
  python3 pending_rewrites.py --brief    pending=N / ids=... for a workflow
  python3 pending_rewrites.py --task     the same as a job to be done

Exit 0 whether or not anything is pending: nothing here is a failure.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FLAGGED = os.path.join(HERE, "flagged_text.json")
CORRECTIONS = os.path.join(HERE, "corrections.json")
POOL = os.path.join(HERE, "pool.json")


def load(path, empty):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return empty


def pending():
    """Flags with no correction written for them yet, newest last."""
    done = load(CORRECTIONS, {})
    out = []
    seen = set()
    for flag in load(FLAGGED, []):
        item = str(flag.get("id") or "")
        if not item or item in done or item in seen:
            continue
        seen.add(item)
        out.append(flag)
    return out


def captions():
    """The caption the pool currently holds for each id."""
    pool = load(POOL, {})
    entries = pool.get("entries") or pool.get("maps") or []
    return {str(e["id"]): e.get("t") or "" for e in entries}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brief", action="store_true",
                    help="key=value lines for a workflow to read")
    ap.add_argument("--task", action="store_true",
                    help="the work to be done, as a prompt would put it")
    args = ap.parse_args()

    waiting = pending()
    now = captions()

    if args.brief:
        print("pending={}".format(len(waiting)))
        print("ids={}".format(",".join(str(f.get("id")) for f in waiting)))
        return 0

    if not waiting:
        print("Nothing waiting: every flagged caption has a correction.")
        return 0

    if args.task:
        print("{} flagged caption{} need a replacement written.\n".format(
            len(waiting), "" if len(waiting) == 1 else "s"))
    for flag in waiting:
        item = str(flag.get("id"))
        source = flag.get("source") or ""
        shown = flag.get("shown") or ""
        here = now.get(item)
        print("- id: {}".format(item))
        print("  detected language: {}".format(flag.get("lang") or "unknown"))
        print("  archive's own words: {}".format(source or "(not recorded)"))
        if shown and shown != source:
            print("  published as: {}".format(shown))
        else:
            print("  published as: the same -- never translated, so a flag "
                  "alone changes nothing a reader sees")
        if here is None:
            print("  NOTE: no longer in the pool; a correction would not apply")
        elif source and here != source:
            print("  NOTE: the pool now says {!r}; the archive re-catalogued "
                  "it, so write the correction against that".format(here))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
