#!/usr/bin/env python3
"""retry-ledger: what did the agent fail at, what fixed it, and what did the detour cost?

    ledger.py scan     <workspaces-root> [workspace-filter] [repo]
    ledger.py classify <workspaces-root> <scan-json>

Reads rote's recorded evidence only. It reports the failure and repair structure it can
prove from that record and never claims a root cause: attributing the first wrong turn in
a trajectory is a judgement the evidence does not carry, and asserting it confidently is
how a diagnosis becomes a wrong one.

Repeated failures of the same program in one workspace are grouped into a single incident.
Five consecutive attempts at the same broken command are one problem, not five, and
listing them separately makes a small failure look like a large one.

`scan` emits only the failed attempts and the later runs of the same program, never the
whole trajectory. A step's captured stdout is previewed at 64 KiB, so a stage that hands
on everything it read stops working the moment a machine has real history.

Never executes anything and never modifies a workspace: it opens files and reads them.
"""
import json, os, re, sys

sys.dont_write_bytecode = True
MAX_WS = 200
MAX_RESP = 5000
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", ".cursorrules", "CONTRIBUTING.md", "README.md")
EPHEMERAL = re.compile(r"/tmp/\.tmp[A-Za-z0-9]{6,}")


def normalise(args):
    """rote unpacks a play into a fresh temp directory on every run, so the same command
    never has byte-identical argv twice. Comparing raw argv reports a repair on every
    single pair. Collapse those paths before comparing; nothing else is rewritten."""
    return [EPHEMERAL.sub("<run-tmp>", a) for a in args]


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
    return {
        "n": int(n) if n else 0,
        "status": resp.get("status"),
        "duration_ms": resp.get("duration_ms") or 0,
        "tokens": toks.get("total_tokens") or 0,
        "program": inv.get("program"),
        "args": [str(a) for a in (inv.get("args") or [])],
        "stderr": (((body.get("stderr") or {}).get("text")) or "")[:300],
        "method": req.get("method"),
    }


def read_instructions(repo):
    """The repaired value may already be written down. Whether a string appears in an
    instruction file is a fact; whether the agent should have known it is not."""
    if not repo or not os.path.isdir(repo):
        return None
    blobs = {}
    for name in INSTRUCTION_FILES:
        p = os.path.join(repo, name)
        if os.path.isfile(p):
            try:
                blobs[name] = open(p, encoding="utf-8", errors="replace").read()[:200000]
            except OSError:
                continue
    return blobs


def scan(root, only=None, repo=None):
    if not os.path.isdir(root):
        sys.stderr.write("error: no such directory: %s\n" % root)
        sys.exit(1)
    try:
        names = sorted(d for d in os.listdir(root) if not d.startswith("."))
    except OSError as e:
        sys.stderr.write("error: cannot list %s: %s\n" % (root, e))
        sys.exit(1)

    out, unreadable, scanned = {}, [], 0
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
        progs = {f["program"] for f in fails if f["program"]}
        cands = [{"n": r["n"], "program": r["program"], "args": r["args"], "status": r["status"]}
                 for r in rows if r["program"] in progs]
        # the prefix boundary is the last step that worked before the first failure;
        # that is an ordering fact, not a claim about what caused anything
        oks = [r["n"] for r in rows if r["status"] == 200]
        out[ws] = {"failures": fails, "candidates": cands, "successes": oks,
                   "first_failure": fails[0]["n"] if fails else None}

    instr = read_instructions(repo)
    return {"root": root, "workspaces": out, "unreadable": unreadable,
            "responses_scanned": scanned, "truncated_workspaces": len(names) > MAX_WS,
            "repo": repo or "", "instructions": instr}


def key_of(program, args):
    """Two attempts are the same command when the program and its argv match. Matching on
    the request method alone would pair unrelated commands, because every process step
    shares one method."""
    return None if not program else (program, tuple(normalise(args)))


def arg_delta(a, b):
    a, b = normalise(a), normalise(b)
    if len(a) != len(b):
        added = [x for x in b if x not in a]
        if added:
            return ("argv %d -> %d, adding %s"
                    % (len(a), len(b), " ".join(x[:40] for x in added[:3])), added[0])
        return "argv length %d -> %d" % (len(a), len(b)), None
    diffs = [(x, y) for x, y in zip(a, b) if x != y]
    if not diffs:
        return "no argv difference", None
    text = "; ".join("%s -> %s" % (x[:70], y[:70]) for x, y in diffs[:3])
    return text, diffs[0][1]


def documented(value, instructions):
    """A repair is documented when the value that fixed it literally appears in an
    instruction file. Absence is reported as absence, never as fault."""
    if instructions is None:
        return "not-checked", ""
    if not value:
        return "no-repair-value", ""
    needle = os.path.basename(value.rstrip("/")) or value
    if len(needle) < 3:
        return "too-short-to-search", ""
    for name, blob in instructions.items():
        if needle in blob:
            return "documented", name
    return "undocumented", ""


def classify(data):
    incidents = []
    totals = {"incidents": 0, "attempts": 0, "wasted_ms": 0, "wasted_tokens": 0}
    instructions = data.get("instructions")

    for ws, blob in (data.get("workspaces") or {}).items():
        cands = blob.get("candidates") or []
        oks = blob.get("successes") or []
        first_failure = blob.get("first_failure")
        fails = sorted(blob.get("failures") or [], key=lambda f: f["n"])

        # group consecutive failures of the same program into one incident
        groups = []
        for f in fails:
            if groups and groups[-1]["program"] == f.get("program"):
                groups[-1]["attempts"].append(f)
            else:
                groups.append({"program": f.get("program"), "attempts": [f]})

        for g in groups:
            first = g["attempts"][0]
            totals["incidents"] += 1
            totals["attempts"] += len(g["attempts"])
            for a in g["attempts"]:
                totals["wasted_ms"] += a.get("duration_ms") or 0
                totals["wasted_tokens"] += a.get("tokens") or 0

            k = key_of(first.get("program"), first.get("args") or [])
            last_n = g["attempts"][-1]["n"]
            later = [c for c in cands if c["n"] > last_n and c["program"] == first.get("program")]
            repair_value, doc, doc_where = None, "not-checked", ""

            if k is None:
                verdict = "INDETERMINATE"
                detail = "no recorded invocation to compare; attempts cannot be paired"
                partner = None
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
                elif repaired:
                    verdict = "REPAIRED_RETRY"
                    detail, repair_value = arg_delta(first.get("args") or [], repaired[0]["args"])
                    partner = repaired[0]["n"]
                    doc, doc_where = documented(repair_value, instructions)
                elif exact:
                    verdict = "UNCHANGED_RETRY"
                    detail = "the identical command was run again and failed again"
                    partner = exact[0]["n"]
                elif len(g["attempts"]) > 1 and all(
                        key_of(a.get("program"), a.get("args") or []) == k
                        for a in g["attempts"]):
                    verdict = "UNCHANGED_RETRY"
                    detail = ("the identical command was run %d times and failed every time"
                              % len(g["attempts"]))
                    partner = g["attempts"][1]["n"]
                else:
                    verdict = "UNRECOVERED"
                    detail = "no later run of %s succeeded in this workspace" % first.get("program")
                    partner = None

            # Regression prefix. Everything here is recorded, never inferred: the freeze
            # point is the last step that succeeded before this incident, the known-bad
            # action is the argv that failed, and a known-good action is only stated when
            # a later run actually succeeded. Where nothing succeeded, that is said.
            before = [n for n in oks if n < first["n"]]
            good = None
            if verdict in ("REPAIRED_RETRY", "TRANSIENT") and partner is not None:
                match = [c for c in cands if c["n"] == partner]
                if match:
                    good = match[0]["args"]
            regression = {
                "freeze_after": max(before) if before else None,
                # normalised for display too: a regression spec that names a dead
                # per-run temp directory is not something anyone can act on
                "known_bad": [first.get("program")] + normalise((first.get("args") or [])[:6]),
                "known_good": ([first.get("program")] + normalise(good[:6])) if good else None,
                "known_good_recorded_at": partner if good else None,
                "note": ("no later run succeeded, so only the action to avoid is known"
                         if good is None else
                         "the known-good action is the argv rote recorded succeeding"),
            }

            incidents.append({
                "workspace": ws,
                "is_first_failure_in_workspace": first["n"] == first_failure,
                "regression": regression,
                "first_response": first["n"],
                "attempts": len(g["attempts"]),
                "responses": [a["n"] for a in g["attempts"]][:10],
                "program": first.get("program"),
                "argv": (first.get("args") or [])[:5],
                "status": first.get("status"),
                "duration_ms": sum(a.get("duration_ms") or 0 for a in g["attempts"]),
                "tokens": sum(a.get("tokens") or 0 for a in g["attempts"]),
                "stderr": (first.get("stderr") or "")[:200],
                "verdict": verdict,
                "detail": detail,
                "paired_with": partner,
                "repair_value": repair_value,
                "documented": doc,
                "documented_in": doc_where,
            })
    return incidents, totals


def main():
    if len(sys.argv) < 3:
        sys.stderr.write("usage: ledger.py scan|classify <workspaces-root> [args]\n")
        sys.exit(2)
    mode = sys.argv[1]
    raw = sys.argv[2]
    # the caller should not have to spell out a home path, and a literal one in the
    # manifest would not be portable to anyone else's machine
    here = os.path.dirname(os.path.abspath(__file__))
    if raw == "demo":
        root = os.path.join(here, "demo", "workspaces")
    else:
        if raw in ("auto", "", "-"):
            raw = os.path.join("~", ".rote", "workspaces")
        root = os.path.abspath(os.path.expanduser(raw))

    if mode == "scan":
        only = sys.argv[3] if len(sys.argv) > 3 else None
        repo = sys.argv[4] if len(sys.argv) > 4 else None
        if repo == "demo":
            repo = os.path.join(here, "demo", "repo")
        elif repo:
            repo = os.path.abspath(os.path.expanduser(repo))
        else:
            repo = None
        print(json.dumps(scan(root, only or None, repo), separators=(",", ":")))
        return

    if len(sys.argv) < 4:
        sys.stderr.write("error: classify needs the scan json\n")
        sys.exit(2)
    data = json.loads(sys.argv[3])
    incidents, totals = classify(data)
    print(json.dumps({
        "root": data.get("root"),
        "repo": data.get("repo", ""),
        "instructions_checked": sorted((data.get("instructions") or {}).keys()),
        "first_failures": {ws: b.get("first_failure")
                           for ws, b in (data.get("workspaces") or {}).items()},
        "workspaces_with_failures": len(data.get("workspaces") or {}),
        "responses_scanned": data.get("responses_scanned", 0),
        "unreadable": data.get("unreadable", []),
        "truncated_workspaces": data.get("truncated_workspaces", False),
        "totals": totals,
        "incidents": sorted(incidents, key=lambda i: (i["workspace"], i["first_response"])),
    }, separators=(",", ":")))


if __name__ == "__main__":
    main()
