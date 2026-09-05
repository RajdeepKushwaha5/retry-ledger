#!/usr/bin/env python3
"""Run the real analyzer against bundled trajectories before it is trusted with yours.

The assertions that prove this analyzer works live on the author's machine, where nobody
running the play can see them. This feeds the shipped ledger.py its own bundled
trajectories at run time, and the presentation withholds its verdict if any case fails.

Two things a naive self-check misses, both because a green check above a broken analyzer
is worse than no check at all:

  every verdict needs a POSITIVE case. A rule with only negative cases can be deleted and
  the check still passes.

  DISCOVERY is checked, not only classification. A scan that reads no response files
  reports a clean history, and no classification case would notice.

  STRUCTURE is checked, not only conclusions. Verdict cases on Linux fixtures cannot see a
  temp-path pattern that matches nothing on macOS, or a payload too large to reach the
  step that reads it. Both shipped past a green check.

    selfcheck.py  ->  JSON {passed, total, failures}
"""
import json, os, subprocess, sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "ledger.py")

# workspace, first response, verdict: what the bundled trajectories must produce
EXPECTED = [
    ("demo-unchanged-retry", 1, "UNCHANGED_RETRY"),
    ("demo-cascade", 1, "REPAIRED_RETRY"),
    ("demo-wrong-directory", 1, "REPAIRED_RETRY"),
    ("demo-cascade", 3, "UNRECOVERED"),
    ("demo-unrecovered", 1, "UNRECOVERED"),
    ("demo-transient", 1, "TRANSIENT"),
    ("demo-indeterminate", 1, "INDETERMINATE"),
]
MUST_COVER = {"UNCHANGED_RETRY", "REPAIRED_RETRY", "TRANSIENT", "UNRECOVERED",
              "INDETERMINATE"}
# the instruction-file check has two answers and both need a case, or one branch could
# be deleted and every repair would silently read the same way
DOCUMENTED = {("demo-cascade", 1): "documented",
              ("demo-wrong-directory", 1): "undocumented"}


def run_json(args):
    # spawned as a literal so the command can be checked against deps.toml
    p = subprocess.run(["python3"] + args, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "").strip()[:200] or "exit %d" % p.returncode)
    return json.loads(p.stdout)


def main():
    failures, total = [], 0
    seen = set()
    scan = None
    got = None

    try:
        scan = run_json([LEDGER, "scan", "demo", "", "demo"])
        out = run_json([LEDGER, "classify", "demo", json.dumps(scan)])
        got = {(i["workspace"], i["first_response"]): i for i in out.get("incidents", [])}
    except Exception as e:
        total += len(EXPECTED)
        failures.append({"case": "analyzer-runs", "detail": str(e)})

    if got is not None:
        for ws, first, verdict in EXPECTED:
            total += 1
            seen.add(verdict)
            row = got.get((ws, first))
            if row is None:
                failures.append({"case": "%s@%d" % (ws, first),
                                 "detail": "expected %s, produced no incident" % verdict})
            elif row["verdict"] != verdict:
                failures.append({"case": "%s@%d" % (ws, first),
                                 "detail": "expected %s, produced %s"
                                           % (verdict, row["verdict"])})
        for key, want in sorted(DOCUMENTED.items()):
            total += 1
            row = got.get(key)
            actual = (row or {}).get("documented")
            if actual != want:
                failures.append({
                    "case": "written-down:%s@%d" % key,
                    "detail": "expected the repair value to be %s in the instruction "
                              "files, got %s" % (want, actual)})
        # the regression prefix must name a real freeze point where one exists
        total += 1
        row = got.get(("demo-cascade", 3))
        freeze = ((row or {}).get("regression") or {}).get("freeze_after")
        if freeze != 2:
            failures.append({"case": "regression:freeze-point",
                             "detail": "expected the last success before the incident to "
                                       "be @2, got %s" % freeze})

    for verdict in sorted(MUST_COVER - seen):
        total += 1
        failures.append({"case": "coverage:%s" % verdict,
                         "detail": "no bundled case asserts this verdict, so removing the "
                                   "rule that produces it would not be noticed"})

    # discovery: a scan that reads nothing reports a clean history, and every case above
    # would still pass on an empty incident set
    total += 1
    n = len((scan or {}).get("workspaces") or {})
    if n < 6:
        failures.append({"case": "discovery:reads-the-trajectories",
                         "detail": "expected at least 6 bundled workspaces with failures, "
                                   "the scan reported %d" % n})

    # Structure, not verdicts. Every case above asserts what the analyzer concludes;
    # these assert that the conclusions can survive the trip to a reader. A payload that
    # does not fit through the step is not a quieter history, and a temp-path pattern that
    # only matches one operating system turns every pair into a false repair on the other.
    sys.path.insert(0, HERE)
    try:
        import ledger
    except Exception as e:
        total += 1
        failures.append({"case": "analyzer-imports", "detail": str(e)})
        ledger = None

    if ledger is not None:
        LINUX = "/tmp/.tmpS0qIfW/resources/audit_dir.py"
        MACOS = "/private/var/folders/qx/8k2n_1/T/.tmpA1b2C3/resources/audit_dir.py"
        ORDINARY = "/home/dev/project/src/main.py"

        # the temp directory is replaced in place, so what follows it survives
        WANT = ["<run-tmp>/resources/audit_dir.py"]
        total += 1
        if ledger.normalise([LINUX]) != WANT:
            failures.append({"case": "portability:linux-temp-dir",
                             "detail": "expected %s, got %s"
                                       % (WANT, ledger.normalise([LINUX]))})
        total += 1
        if ledger.normalise([MACOS]) != WANT:
            failures.append({
                "case": "portability:macos-temp-dir",
                "detail": "expected %s, got %s. rote unpacks into /private/var/folders on "
                          "macOS, so anchoring this on /tmp makes normalise an identity "
                          "function there and every retried command looks like a repair"
                          % (WANT, ledger.normalise([MACOS]))})
        total += 1
        if ledger.normalise([LINUX]) != ledger.normalise([MACOS]):
            failures.append({"case": "portability:same-command-same-string",
                             "detail": "the same command recorded on Linux and on macOS "
                                       "normalised differently, so identity depends on the "
                                       "operating system that produced the trajectory"})
        total += 1
        if ledger.normalise([ORDINARY]) != [ORDINARY]:
            failures.append({"case": "portability:ordinary-path-untouched",
                             "detail": "a path that is not a run temp directory was "
                                       "rewritten to %s, so real argv differences would "
                                       "be hidden" % ledger.normalise([ORDINARY])})

        # identity must read the full argv, never the shortened display copy
        head = "x" * (ledger.ARG_CHARS + 40)
        a = ledger.argv_key("bash", ["-lc", head + "ONE"])
        b = ledger.argv_key("bash", ["-lc", head + "TWO"])
        total += 1
        if a == b:
            failures.append({
                "case": "identity:long-argv-not-merged",
                "detail": "two commands differing only past the display cut produced the "
                          "same identity, so a shortened argv would report an identical "
                          "retry that never happened"})
        total += 1
        if ledger.argv_key("bash", ["-lc", head + "ONE"]) != a:
            failures.append({"case": "identity:stable",
                             "detail": "the same command produced two different identities"})

        total += 1
        shown = ledger.shorten(head)
        if len(shown) > ledger.ARG_CHARS + len(ledger.ELIDED) or not shown.endswith(ledger.ELIDED):
            failures.append({"case": "shortening:marks-what-it-cut",
                             "detail": "a long argv element was not cut and marked: %d chars"
                                       % len(shown)})
        total += 1
        if ledger.shorten("short") != "short":
            failures.append({"case": "shortening:leaves-short-values-alone",
                             "detail": "a value that fits was rewritten anyway"})

        # An interpreter's program name says nothing about which command ran. A real run
        # reported python3 selfcheck.py as the repair for python3 blast.py, which is a
        # claim the evidence does not carry.
        blast = {"program": "python3", "args": ["/w/resources/blast.py", "signatures", "."]}
        selfck = {"program": "python3", "args": ["/w/resources/selfcheck.py"]}
        total += 1
        if ledger.script_of(blast) == ledger.script_of(selfck):
            failures.append({
                "case": "pairing:different-scripts-are-different-commands",
                "detail": "two unrelated Python scripts share an identity, so a later run "
                          "of one would be reported as the repair for the other"})
        total += 1
        if ledger.script_of(blast) != "blast.py":
            failures.append({"case": "pairing:script-is-found",
                             "detail": "expected blast.py, got %s" % ledger.script_of(blast)})

        # the other direction: an ordinary program's first argument is a subcommand or a
        # flag value, and requiring it to match would delete real repairs
        npm_bad = {"program": "npm", "args": ["test"]}
        npm_good = {"program": "npm", "args": ["--prefix", "apps/api", "test"]}
        total += 1
        if ledger.script_of(npm_bad) is not None or ledger.script_of(npm_good) is not None:
            failures.append({
                "case": "pairing:non-interpreters-unaffected",
                "detail": "the interpreter rule was applied to npm, which would stop "
                          "`npm test` pairing with `npm --prefix apps/api test` and remove "
                          "the repairs this play exists to report"})

        # a cap on what is shown must say that it capped
        total += 1
        many_a = ["cmd", "a1", "a2", "a3", "a4", "a5"]
        many_b = ["cmd", "b1", "b2", "b3", "b4", "b5"]
        text, _ = ledger.arg_delta(many_a, many_b)
        if "not shown" not in text:
            failures.append({
                "case": "arg-delta:says-what-it-left-out",
                "detail": "five argument differences rendered as %r, which reads as the "
                          "whole change when it is three of five" % text})
        total += 1
        few, _ = ledger.arg_delta(["cmd", "a1"], ["cmd", "b1"])
        if "not shown" in few:
            failures.append({"case": "arg-delta:no-false-truncation-notice",
                             "detail": "a complete list claimed it had left something out"})

        # the payload has to pass through a step whose captured stdout is previewed at
        # 64 KiB, and is handed to classify as an argument on top of that
        total += 1
        if ledger.BUDGET >= 65536:
            failures.append({"case": "budget:under-the-step-ceiling",
                             "detail": "the scan budget is %d, at or above rote's 65536 "
                                       "byte preview" % ledger.BUDGET})
        total += 1
        payload = len(json.dumps(scan or {}, separators=(",", ":")))
        if payload > ledger.BUDGET:
            failures.append({"case": "budget:demo-payload-fits",
                             "detail": "the bundled scan emitted %d bytes against a %d "
                                       "byte budget" % (payload, ledger.BUDGET)})

    print(json.dumps({"passed": total - len(failures), "total": total,
                      "failures": failures[:10]}, separators=(",", ":")))


if __name__ == "__main__":
    main()
