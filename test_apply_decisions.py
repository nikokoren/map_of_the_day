#!/usr/bin/env python3
"""
Check apply_decisions.py: what it accepts, what it refuses, and that a
refusal leaves curation.json untouched.

This path turns text from a comment box into what the plugin serves, so
the interesting cases are the refusals. Each one runs the real script
against a throwaway copy of the repo -- its own pool.json and
curation.json in a temp directory -- so nothing here can touch the live
files.

Run it with no arguments from the repo root.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "apply_decisions.py")
IDS = ["a", "b", "c", "d"]
BASE = {"vetoed": ["a"], "untranslate": [], "reviewed_to": "2026-10-10"}


def kind():
    """The queue name the script under test answers to."""
    for line in open(SCRIPT):
        if line.startswith("KIND"):
            return line.split("=", 1)[1].strip().strip('"\'')
    raise SystemExit("no KIND in " + SCRIPT)


def run(block, curation=None, raw=None):
    """Run the script in a scratch copy; give back (rc, output, curation, flagged)."""
    tmp = tempfile.mkdtemp()
    try:
        shutil.copy(SCRIPT, tmp)
        with open(os.path.join(tmp, "pool.json"), "w") as fh:
            json.dump({"maps": [{"id": i} for i in IDS]}, fh)
        with open(os.path.join(tmp, "curation.json"), "w") as fh:
            json.dump(curation if curation is not None else BASE, fh)
        if raw is not None:
            body = raw
        elif block is None:
            body = "just a comment, no block"
        else:
            body = "before\n\n```queue\n{}\n```\n\nafter".format(json.dumps(block))
        done = subprocess.run([sys.executable, os.path.join(tmp, "apply_decisions.py")],
                              input=body, capture_output=True, text=True)
        with open(os.path.join(tmp, "curation.json")) as fh:
            after = json.load(fh)
        flagged = os.path.join(tmp, "flagged_text.json")
        notes = json.load(open(flagged)) if os.path.exists(flagged) else []
        return done.returncode, (done.stdout + done.stderr).strip(), after, notes
    finally:
        shutil.rmtree(tmp)


def main():
    this = kind()
    fails = []

    def check(name, ok, detail=""):
        print(("  ok   " if ok else "  FAIL ") + name +
              ("\n         " + detail if detail and not ok else ""))
        if not ok:
            fails.append(name)

    def block(**kw):
        out = {"queue": this, "to": "2026-10-24", "reviewed_to": "2026-10-24",
               "vetoed": [], "untranslate": [], "starred": []}
        out.update(kw)
        return out

    print("accepts:")
    # The queue window is a week or a fortnight wide and slides every
    # morning, so a pass done today often ends *earlier* than a window
    # reviewed a week ago -- and is still the first look at what the
    # reshuffle moved into it. Refusing it threw the vetoes away.
    rc, out, after, _ = run(block(**{"from": "2026-10-02", "to": "2026-10-08",
                                     "reviewed_to": "2026-10-08",
                                     "vetoed": ["b", "c"]}))
    check("a pass inside an already-reviewed window", rc == 0, out)
    check("  keeps its vetoes", after["vetoed"] == ["a", "b", "c"], str(after))
    check("  and does not walk reviewed_to back",
          after["reviewed_to"] == "2026-10-10", str(after))

    rc, out, after, _ = run(block(vetoed=["d"]))
    check("a pass past the mark moves it forward",
          rc == 0 and after["reviewed_to"] == "2026-10-24", out + str(after))

    same = block(vetoed=["b"])
    rc1, _, one, _ = run(same)
    rc2, out2, two, _ = run(same, curation=one)
    check("re-pasting the same block changes nothing the second time",
          rc1 == 0 and rc2 == 0 and one == two, "{} vs {}".format(one, two))
    check("  and reports +0", "(+0)" in out2, out2)

    rc, out, after, _ = run(block())
    check("an empty block cannot un-veto", after["vetoed"] == ["a"], str(after))

    rc, out, after, notes = run(block(untranslate=[
        {"id": "b", "title": "Grisons", "source": "Graubünden", "lang": "de"}]))
    check("a text flag is recorded", rc == 0 and after["untranslate"] == ["b"],
          out + str(after))
    check("  and keeps the source line to diagnose from",
          len(notes) == 1 and notes[0]["source"] == "Graubünden", str(notes))

    # A caption that was never translated is the case most in need of a
    # replacement -- there is no translation to drop, so a flag changes
    # nothing a reader sees until somebody writes one. These used to be
    # thrown away for carrying no separate source.
    rc, out, after, notes = run(block(untranslate=[
        {"id": "c", "lang": "", "source": "Vue de la Place Royale",
         "shown": "Vue de la Place Royale"}]))
    check("an untranslated caption can be flagged",
          rc == 0 and after["untranslate"] == ["c"], out + str(after))
    check("  and reaches the diagnostic queue",
          len(notes) == 1 and notes[0]["shown"] == "Vue de la Place Royale",
          str(notes))
    rc, out, after, notes = run(block(untranslate=[
        {"id": "d", "lang": "", "source": "", "shown": "Ozero Baĭkal"}]))
    check("  even with no source field at all",
          rc == 0 and len(notes) == 1, out + str(notes))

    print("stars:")
    rc, out, after, _ = run(block(starred=["b", "c"]))
    check("a star is recorded", rc == 0 and after.get("starred") == ["b", "c"],
          out + str(after))
    rc, out, after, _ = run(block(starred=["b"]),
                            curation=dict(BASE, starred=["c"]))
    check("stars union with what is already there",
          after.get("starred") == ["b", "c"], str(after))
    # Changing your mind can only ever mean "keep it out".
    rc, out, after, _ = run(block(vetoed=["c"], starred=["c"]))
    check("a veto in the same block beats a star",
          after["vetoed"] == ["a", "c"] and after.get("starred") == [], str(after))
    rc, out, after, _ = run(block(starred=["a"]))
    check("and beats one already in force",
          after.get("starred") == [], str(after))
    rc, out, after, _ = run(block(starred=["b", "nope"]))
    check("an unknown id refuses the whole block",
          rc == 1 and "not in the pool" in out, out)
    check("  and nothing is written", after == BASE, str(after))

    print("refuses, and writes nothing:")
    for name, kwargs, want in (
            ("a comment with no block", {"block": None}, "no ```queue block"),
            ("a block for the other queue",
             {"block": dict(block(), queue="something-else")}, "this repo is"),
            ("ids that are not in the pool",
             {"block": block(vetoed=["b", "nope"])}, "not in the pool"),
            ("a block that is not valid JSON",
             {"raw": "```queue\n{not json,}\n```", "block": None}, "not valid JSON"),
    ):
        rc, out, after, _ = run(**kwargs)
        check(name, rc == 1 and want in out, out)
        check("  curation.json untouched", after == BASE, str(after))

    print("all clear" if not fails else "{} failed".format(len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
