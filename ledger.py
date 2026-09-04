#!/usr/bin/env python3
"""retry-ledger: what did the agent fail at, what fixed it, and what did the detour cost?

    ledger.py scan     <workspaces-root> [workspace-filter]
    ledger.py classify <workspaces-root> <scan-json>

Reads rote's recorded evidence only. It reports the failure and repair structure it can
prove from that record and never claims a root cause: attributing the first wrong turn in
a trajectory is a judgement the evidence does not carry, and asserting it confidently is
how a diagnosis becomes a wrong one.

`scan` deliberately emits only the failed attempts and the later runs of the same program,
never the whole trajectory. A step's captured stdout is previewed at 64 KiB, so a stage
that hands on everything it read stops working the moment a machine has real history.

Never executes anything and never modifies a workspace: it opens JSON and reads it.
"""
import json, os, re, sys

sys.dont_write_bytecode = True
MAX_WS = 200
MAX_RESP = 5000
MAX_CANDIDATES = 12


def load_response(path):
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    req = d.get("request") or {}
    resp = d.get("response") or {}
    body = resp.get("body") if isinstance(resp.get("body"), dict) else {}
    inv = body.get("invocation") or {}
    toks = d.get("tokens") or {}
    n = re.sub(r"\D", "", str(d.get("id", "")))
    stderr = ((body.get("stderr") or {}).get("text") or "")
    return {
        "n": int(n) if n else 0,
        "ts": d.get("timestamp", ""),
        "status": resp.get("status"),
        "duration_ms": resp.get("duration_ms") or 0,
        "tokens": toks.get("total_tokens") or 0,
        "program": inv.get("program"),
        "args": [str(a) for a in (inv.get("args") or [])],
        "stderr": stderr[:300],
        "method": req.get("method"),
    }


def scan(root, only=None):
    if not os.path.isdir(root):
        sys.stderr.write("error: no such directory: %s\n" % root)
        sys.exit(1)
    try:
        names = sorted(d for d in os.listdir(root) if not d.startswith("."))
    except OSError as e:
        sys.stderr.write("error: cannot list %s: %s\n" % (root, e))
        sys.exit(1)

    out, unreadable = {}, []
    scanned = 0
    for ws in names[:MAX_WS]:
        if only and only not in ws:
            continue
        rdir = os.path.join(root, ws, ".rote", "responses")
        if not os.path.isdir(rdir):
            continue
        try:
            files = sorted(os.listdir(rdir))
        except OSError as e:
            unreadable.append({"path": ws, "reason": type(e).__name__})
            continue
        rows = []
        for fn in files[:MAX_RESP]:
            if not fn.endswith(".json"):
                continue
            r = load_response(os.path.join(rdir, fn))
            if r is None:
                unreadable.append({"path": os.path.join(ws, fn), "reason": "unparseable"})
                continue
            rows.append(r)
        if not rows:
            continue
        scanned += len(rows)
        rows.sort(key=lambda r: r["n"])
        fails = [r for r in rows if r["status"] not in (None, 200)]
        if not fails:
            continue
        # only the later runs of a failed program are pairing candidates
        progs = {f["program"] for f in fails if f["program"]}
        cands = [{"n": r["n"], "program": r["program"], "args": r["args"],
                  "status": r["status"]}
                 for r in rows if r["program"] in progs][:MAX_CANDIDATES * len(progs) or 1]
        out[ws] = {"failures": fails, "candidates": cands}
    return {"root": root, "workspaces": out, "unreadable": unreadable,
            "responses_scanned": scanned,
            "truncated_workspaces": len(names) > MAX_WS}


EPHEMERAL = re.compile(r"/tmp/\.tmp[A-Za-z0-9]{6,}")


def normalise(args):
    """rote unpacks a play into a fresh temp directory on every run, so the same command
    never has byte-identical argv twice. Comparing raw argv reports a repair on every
    single pair. Collapse those paths before comparing; nothing else is rewritten."""
    return [EPHEMERAL.sub("<run-tmp>", a) for a in args]


def key_of(program, args):
    """Two attempts are the same command when the program and its argv match. Matching on
    the request method alone would pair unrelated commands, because every process step
    shares one method."""
    return None if not program else (program, tuple(normalise(args)))


def arg_delta(a, b):
    a, b = normalise(a), normalise(b)
    if len(a) != len(b):
        return "argv length %d -> %d" % (len(a), len(b))
    diffs = ["%s -> %s" % (x, y) for x, y in zip(a, b) if x != y]
    return "; ".join(d[:120] for d in diffs[:3]) if diffs else "no argv difference"


def classify(data):
    findings = []
    totals = {"failures": 0, "wasted_ms": 0, "wasted_tokens": 0}
    for ws, blob in (data.get("workspaces") or {}).items():
        cands = blob.get("candidates") or []
        for f in blob.get("failures") or []:
            totals["failures"] += 1
            totals["wasted_ms"] += f.get("duration_ms") or 0
            totals["wasted_tokens"] += f.get("tokens") or 0
            k = key_of(f.get("program"), f.get("args") or [])
            later = [c for c in cands if c["n"] > f["n"] and c["program"] == f.get("program")]
            if k is None:
                verdict, detail, partner = ("INDETERMINATE",
                                            "no recorded invocation to compare; attempts cannot be paired",
                                            None)
            else:
                exact = [c for c in later if key_of(c["program"], c["args"]) == k]
                exact_ok = [c for c in exact if c["status"] == 200]
                repaired = [c for c in later
                            if key_of(c["program"], c["args"]) != k and c["status"] == 200]
                if exact_ok:
                    verdict = "TRANSIENT"
                    detail = ("the identical argv succeeded later; if the command reads a "
                              "script or file that was edited in between, that repair is "
                              "not visible from argv alone")
                    partner = exact_ok[0]["n"]
                elif exact:
                    verdict = "UNCHANGED_RETRY"
                    detail = "the identical command was run again and failed again"
                    partner = exact[0]["n"]
                elif repaired:
                    verdict = "REPAIRED_RETRY"
                    detail = arg_delta(f.get("args") or [], repaired[0]["args"])
                    partner = repaired[0]["n"]
                else:
                    verdict = "UNRECOVERED"
                    detail = "no later run of %s succeeded in this workspace" % f.get("program")
                    partner = None
            findings.append({
                "workspace": ws, "response": f["n"], "program": f.get("program"),
                "argv": (f.get("args") or [])[:5], "status": f.get("status"),
                "duration_ms": f.get("duration_ms") or 0, "tokens": f.get("tokens") or 0,
                "stderr": (f.get("stderr") or "")[:200],
                "verdict": verdict, "detail": detail, "paired_with": partner,
            })
    return findings, totals


def main():
    if len(sys.argv) < 3:
        sys.stderr.write("usage: ledger.py scan|classify <workspaces-root> [arg]\n")
        sys.exit(2)
    mode = sys.argv[1]
    raw = sys.argv[2]
    # AUTO_ROOT: the caller should not have to spell out a home path, and a literal one
    # in the manifest would not be portable to anyone else's machine.
    if raw in ("auto", "", "-"):
        raw = os.path.join("~", ".rote", "workspaces")
    root = os.path.abspath(os.path.expanduser(raw))
    if mode == "scan":
        only = sys.argv[3] if len(sys.argv) > 3 else None
        print(json.dumps(scan(root, only or None), separators=(",", ":")))
        return
    if len(sys.argv) < 4:
        sys.stderr.write("error: classify needs the scan json\n")
        sys.exit(2)
    data = json.loads(sys.argv[3])
    findings, totals = classify(data)
    print(json.dumps({
        "root": data.get("root"),
        "workspaces_with_failures": len(data.get("workspaces") or {}),
        "responses_scanned": data.get("responses_scanned", 0),
        "unreadable": data.get("unreadable", []),
        "truncated_workspaces": data.get("truncated_workspaces", False),
        "totals": totals,
        "findings": sorted(findings, key=lambda f: (f["workspace"], f["response"])),
    }, separators=(",", ":")))


if __name__ == "__main__":
    main()
