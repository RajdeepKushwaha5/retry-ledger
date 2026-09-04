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

    print(json.dumps({"passed": total - len(failures), "total": total,
                      "failures": failures[:10]}, separators=(",", ":")))


if __name__ == "__main__":
    main()
