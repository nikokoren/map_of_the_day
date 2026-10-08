#!/usr/bin/env python3
"""
Check what pending_rewrites.py reports.

Two things branch on it -- the reminder decides whether to open an issue,
and the rewrite workflow decides whether to spend a run -- so a wrong
count is either a caption left wrong in the feed or a job that burns a
runner for nothing.

Nothing here touches the real files.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "pending_rewrites.py")


def run(flagged, corrections, pool=None, args=()):
    tmp = tempfile.mkdtemp()
    with open(os.path.join(tmp, "flagged_text.json"), "w") as fh:
        json.dump(flagged, fh)
    with open(os.path.join(tmp, "corrections.json"), "w") as fh:
        json.dump(corrections, fh)
    with open(os.path.join(tmp, "pool.json"), "w") as fh:
        json.dump({"entries": pool if pool is not None else []}, fh)
    import shutil
    shutil.copy(SCRIPT, tmp)
    done = subprocess.run(
        [sys.executable, os.path.join(tmp, "pending_rewrites.py")] + list(args),
        capture_output=True, text=True)
    shutil.rmtree(tmp)
    return done.returncode, done.stdout


def main():
    fails = []

    def check(name, ok, detail=""):
        print(("  ok   " if ok else "  FAIL ") + name +
              ("\n         " + detail if detail and not ok else ""))
        if not ok:
            fails.append(name)

    flag = lambda i, src, shown=None, lang="fr": {
        "id": i, "lang": lang, "source": src,
        "shown": shown if shown is not None else src}

    rc, out = run([flag("a", "Une fontana", "A fontana")], {}, args=["--brief"])
    check("one flag, no correction", rc == 0 and "pending=1" in out, out)
    check("  and names it", "ids=a" in out, out)

    rc, out = run([flag("a", "Une fontana")], {"a": {"title": "A fountain"}},
                  args=["--brief"])
    check("a flag with a correction is not pending", "pending=0" in out, out)

    rc, out = run([flag("a", "x"), flag("a", "x")], {}, args=["--brief"])
    check("the same id flagged twice counts once", "pending=1" in out, out)

    rc, out = run([], {}, args=["--brief"])
    check("nothing flagged", rc == 0 and "pending=0" in out, out)
    rc, out = run([], {})
    check("  and says so in words", "Nothing waiting" in out, out)

    # Never translated: source and shown are the same, and that is the
    # case a flag cannot repair on its own.
    rc, out = run([flag("a", "Vue de la Place Royale")], {})
    check("an untranslated caption is explained, not just listed",
          "never translated" in out, out)

    # The pool moved underneath a flag.
    rc, out = run([flag("a", "Old caption", "Old caption")], {},
                  pool=[{"id": "a", "t": "New caption"}])
    check("a caption that has since changed is called out",
          "re-catalogued" in out, out)
    rc, out = run([flag("a", "Gone", "Gone")], {}, pool=[{"id": "b", "t": "x"}])
    check("an id no longer in the pool is called out",
          "no longer in the pool" in out, out)

    # A malformed file must not take the reminder down with it.
    tmp = tempfile.mkdtemp()
    import shutil
    shutil.copy(SCRIPT, tmp)
    open(os.path.join(tmp, "flagged_text.json"), "w").write("{not json")
    done = subprocess.run([sys.executable, os.path.join(tmp, "pending_rewrites.py"),
                           "--brief"], capture_output=True, text=True)
    shutil.rmtree(tmp)
    check("a broken flag file reports nothing rather than failing",
          done.returncode == 0 and "pending=0" in done.stdout,
          done.stdout + done.stderr)

    print("all clear" if not fails else "{} failed".format(len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
