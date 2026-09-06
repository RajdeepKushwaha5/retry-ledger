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
import hashlib, json, os, re, sys

sys.dont_write_bytecode = True
MAX_WS = 200
MAX_RESP = 5000
ARG_CHARS = 220      # per argv element kept for display; identity uses the full value
MAX_ARGV = 40        # argv elements kept for display
BUDGET = 56000       # scan payload ceiling, under rote's 65536 with room for the wrapper
ELIDED = "<...>"     # marks a value this play shortened, so it is never read as the value
DIGEST = 12          # hex chars of the argv digest; 48 bits over a few hundred commands
SHOW_DIFFS = 3       # argv differences rendered; any beyond this are counted, not dropped
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", ".cursorrules", "CONTRIBUTING.md", "README.md")
# A run temp directory is ".tmp" plus random characters, and it is NOT always under
# /tmp: macOS puts it at /private/var/folders/<x>/<y>/T/.tmpXXXXXX. Anchoring on
# /tmp made this an identity function on macOS, where every pair then looked like a
# repair. Match the whole path prefix ending in the temp directory, anywhere.
# Built from chr(92) rather than written as an escape. The first attempt at this fix
# shipped "[/\\]\\.tmp..." as source, which Python reads as a single character
# class -- \\] inside a class is an escaped bracket, so the class ran on and {6,}
# applied to it. That pattern matched every argv element, which would have made every
# retry look identical. There is no escape in this source line to be flattened.
_BS = chr(92)
# Anchored on the literal ".tmp" rather than a leading wildcard. The first version of
# this began "[^ ]*", which made re.sub retry at every position of every argv element,
# and one element here can be a 75 KB script: 81 of 82 seconds of an auto scan were
# spent inside this one call. Nothing needs skipping over, because each argv element is
# already a separate string, so the prefix is sliced off instead of matched.
EPHEMERAL = re.compile("[/" + _BS + _BS + "]" + _BS + ".tmp[A-Za-z0-9]{6,}")


def normalise(args):
    """rote unpacks a play into a fresh temp directory on every run, so the same command
    never has byte-identical argv twice. Comparing raw argv reports a repair on every
    single pair. Collapse those paths before comparing; nothing else is rewritten."""
    out = []
    for a in args:
        m = EPHEMERAL.search(a)
        # everything up to and including the run temp directory is the part that differs
        # between runs; what follows it is the same command every time
        out.append(("<run-tmp>" + a[m.end():]) if m else a)
    return out


def shorten(a):
    """Display form of one argv element. An agent that runs a script through `bash -lc`
    puts the whole script in argv, and one such record measured 75,523 bytes against a
    65536 byte ceiling. The marker is deliberate: a shortened value must never be read
    back as the value itself."""
    return a if len(a) <= ARG_CHARS else a[:ARG_CHARS] + ELIDED


def argv_key(program, args):
    """Identity of a command, computed on the FULL argv before any shortening. Comparing
    shortened argv would call two different long scripts the same command and report a
    repeat that never happened."""
    if not program:
        return None
    joined = chr(0).join(normalise(args))
    h = hashlib.sha1(joined.encode("utf-8", "replace")).hexdigest()[:DIGEST]
    return program + ":" + h


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
    full_args = [str(a) for a in (inv.get("args") or [])]
    n = re.sub(r"\D", "", str(d.get("id", "")))
    return {
        "n": int(n) if n else 0,
        "status": resp.get("status"),
        "duration_ms": resp.get("duration_ms") or 0,
        "tokens": toks.get("total_tokens") or 0,
        "program": inv.get("program"),
        "args": [shorten(str(a)) for a in full_args[:MAX_ARGV]],
        "args_key": argv_key(inv.get("program"), full_args),
        "args_elided": (True if (len(full_args) > MAX_ARGV
                                 or any(len(a) > ARG_CHARS for a in full_args)) else None),
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
        # Every response sharing a program with a failure, kept whole. Dropping one can
        # remove the successful run that repaired a failure, and the incident then reads
        # UNRECOVERED -- a claim the evidence does not support. Size is bought below, by
        # shortening what each record displays, never by thinning this list.
        # Pairing only ever looks forward, at candidates after the last attempt of an
        # incident, and no incident starts before the workspace's first failure. So a
        # candidate at or before that point can never be compared against anything, and
        # a workspace with no failures has no incidents to compare at all. Dropping those
        # is free; dropping a candidate AFTER a failure would not be, because it can be
        # the run that repaired it, and losing it reports a false UNRECOVERED.
        floor = fails[0]["n"] if fails else None
        cands = []
        for r in rows:
            if floor is None or r["program"] not in progs or r["n"] <= floor:
                continue
            c = {"n": r["n"], "program": r["program"], "args": r["args"],
                 "args_key": r["args_key"], "status": r["status"]}
            if r["args_elided"]:
                c["args_elided"] = True
            cands.append(c)
        # the prefix boundary is the last step that worked before the first failure;
        # that is an ordering fact, not a claim about what caused anything
        oks = [r["n"] for r in rows if r["status"] == 200]
        out[ws] = {"failures": fails, "candidates": cands, "successes": oks,
                   "first_failure": fails[0]["n"] if fails else None}

    # Last resort. Everything above shortens what a record displays; if the payload is
    # still over the ceiling, whole workspaces are dropped and named. A workspace that
    # survives is reported completely, so no verdict inside one is ever affected by size.
    omitted = []
    order = sorted(out, key=lambda w: (len(out[w]["failures"]) > 0,
                                       -len(json.dumps(out[w]))))
    while len(json.dumps(out)) > BUDGET and len(out) > 1:
        drop = order.pop(0)
        del out[drop]
        omitted.append(drop)

    instr = read_instructions(repo)
    return {"root": root, "workspaces": out, "unreadable": unreadable,
            "responses_scanned": scanned, "truncated_workspaces": len(names) > MAX_WS,
            "omitted_workspaces": omitted,
            "repo": repo or "", "instructions": instr}


# For these, the program name says nothing about which command ran: every Python script
# on the machine is "python3". The script in argv is part of the command's identity.
INTERPRETERS = ("python", "python3", "node", "nodejs", "ruby", "perl", "bash", "sh",
                "zsh", "deno", "bun")


def script_of(row):
    """The script an interpreter was asked to run, or None when the program is not an
    interpreter. Used to keep two unrelated scripts from being paired as a failure and its
    repair merely because both were launched by python3."""
    prog = os.path.basename(str(row.get("program") or ""))
    if prog not in INTERPRETERS:
        return None
    for a in normalise(row.get("args") or []):
        if a.startswith("-"):
            continue
        return os.path.basename(a.rstrip("/"))
    return None


def key_of(row):
    """Two attempts are the same command when the program and its argv match. Matching on
    the request method alone would pair unrelated commands, because every process step
    shares one method. Takes the whole row so identity reads the digest of the full argv
    rather than the shortened display copy."""
    if not row.get("program"):
        return None
    return row.get("args_key") or argv_key(row["program"], row.get("args") or [])


def arg_delta(a, b):
    a, b = normalise(a), normalise(b)
    if len(a) != len(b):
        added = [x for x in b if x not in a]
        if added:
            more = ("" if len(added) <= SHOW_DIFFS
                    else " (and %d more not shown)" % (len(added) - SHOW_DIFFS))
            return ("argv %d -> %d, adding %s%s"
                    % (len(a), len(b),
                       " ".join(x[:40] for x in added[:SHOW_DIFFS]), more), added[0])
        return "argv length %d -> %d" % (len(a), len(b)), None
    diffs = [(x, y) for x, y in zip(a, b) if x != y]
    if not diffs:
        return "no argv difference", None
    text = "; ".join("%s -> %s" % (x[:70], y[:70]) for x, y in diffs[:SHOW_DIFFS])
    if len(diffs) > SHOW_DIFFS:
        # an unannounced cap reads as "the command changed in three ways"
        text += " (and %d more difference(s) not shown)" % (len(diffs) - SHOW_DIFFS)
    return text, diffs[0][1]


def documented(value, instructions):
    """A repair is documented when the value that fixed it literally appears in an
    instruction file. Absence is reported as absence, never as fault."""
    if instructions is None:
        return "not-checked", ""
    if not value:
        return "no-repair-value", ""
    if ELIDED in value:
        # This play shortened the value, so a failed search would say more about the
        # shortening than about the instruction file. Decline instead of claiming.
        return "value-elided", ""
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

        # Group consecutive failures of the same command into one incident. Same
        # program, and for an interpreter the same script, for the same reason pairing
        # needs it: otherwise two different failing Python scripts merge into one
        # incident and the count of distinct problems comes out wrong.
        groups = []
        for f in fails:
            sig = (f.get("program"), script_of(f))
            if groups and groups[-1]["sig"] == sig:
                groups[-1]["attempts"].append(f)
            else:
                groups.append({"sig": sig, "program": f.get("program"), "attempts": [f]})

        for g in groups:
            first = g["attempts"][0]
            totals["incidents"] += 1
            totals["attempts"] += len(g["attempts"])
            for a in g["attempts"]:
                totals["wasted_ms"] += a.get("duration_ms") or 0
                totals["wasted_tokens"] += a.get("tokens") or 0

            k = key_of(first)
            last_n = g["attempts"][-1]["n"]
            # Same program, and for an interpreter the same script: python3 running
            # selfcheck.py is not a later run of python3 running blast.py, and pairing
            # them reported a repair that never happened.
            want_script = script_of(first)
            later = [c for c in cands
                     if c["n"] > last_n and c["program"] == first.get("program")
                     and script_of(c) == want_script]
            repair_value, doc, doc_where = None, "not-checked", ""

            if k is None:
                verdict = "INDETERMINATE"
                detail = "no recorded invocation to compare; attempts cannot be paired"
                partner = None
            else:
                exact = [c for c in later if key_of(c) == k]
                exact_ok = [c for c in exact if c["status"] == 200]
                repaired = [c for c in later
                            if key_of(c) != k and c["status"] == 200]
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
                        key_of(a) == k
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
    if raw not in ("demo", "auto", "", "-") and not os.path.isabs(raw):
        sys.stderr.write("workspaces_root must be an ABSOLUTE path, or auto or demo. Got: " + raw + chr(10) + "A step runs inside rote's own workspace, not the directory you were standing in, so a relative path silently scans the wrong tree. There is no correct fallback: the step cannot see your shell directory." + chr(10))
        sys.exit(2)
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
        got = scan(root, only or None, repo)
        # The heading is what a person reads. Without this the bundled run says
        # "7 incidents across 6 workspaces", which is indistinguishable from their own
        # history -- and rote accepts an unknown parameter name silently, so a typo of
        # workspaces_root lands exactly here.
        got["root_label"] = ("the bundled example trajectories" if raw == "demo"
                             else root)
        print(json.dumps(got, separators=(",", ":")))
        return

    if len(sys.argv) < 4:
        sys.stderr.write("error: classify needs the scan json\n")
        sys.exit(2)
    data = json.loads(sys.argv[3])
    incidents, totals = classify(data)
    print(json.dumps({
        "root": data.get("root"),
        # carried through so the heading can say whose history this is
        "root_label": data.get("root_label", ""),
        "repo": data.get("repo", ""),
        "instructions_checked": sorted((data.get("instructions") or {}).keys()),
        "first_failures": {ws: b.get("first_failure")
                           for ws, b in (data.get("workspaces") or {}).items()},
        "workspaces_with_failures": len(data.get("workspaces") or {}),
        "responses_scanned": data.get("responses_scanned", 0),
        "unreadable": data.get("unreadable", []),
        "truncated_workspaces": data.get("truncated_workspaces", False),
        # dropped by the payload budget, not absent from the history
        "omitted_workspaces": data.get("omitted_workspaces") or [],
        "totals": totals,
        "incidents": sorted(incidents, key=lambda i: (i["workspace"], i["first_response"])),
    }, separators=(",", ":")))


if __name__ == "__main__":
    main()
