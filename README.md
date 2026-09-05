# retry-ledger

**What did your agent fail at, what fixed it, and what did the detour cost?**

Rote records every step your agent took, including the ones that did not work. This reads
that record and reports the failure and repair structure it can prove.

```bash
rote play run https://play.modiqo.ai/rajdeepkushwaha/retry-ledger workspaces_root=demo repo=demo
```

That runs the bundled trajectories, so it works on a clean machine with nothing set up.
For your own history use `workspaces_root=auto`.

Zero credentials. Reads files only: it never executes anything and never modifies a
workspace. `python3`, nothing else.

## Verdicts

| verdict | what is being claimed |
|---|---|
| `UNCHANGED_RETRY` | The identical command was run again and failed again. |
| `REPAIRED_RETRY` | Something changed before it succeeded. The argument that changed is named, **and so is whether that value already appears in your instruction files**. |
| `UNRECOVERED` | It failed and no later run of the same program succeeded. |
| `TRANSIENT` | The same argv succeeded later. **Not proof the work was identical** — a script at the same path may have been rewritten in between. |
| `INDETERMINATE` | The attempts could not be paired. Not a claim that anything was fine. |

Repeated failures of the same program are grouped into **one incident**. Five attempts at
the same broken command are one problem, and listing them five times makes a small failure
look like a large one.

## Regression prefix

Every incident carries one, and every field in it was recorded rather than inferred:

```
regression prefix:
    freeze      : after @1
    known bad   : python3 <run-tmp>/resources/audit_dir.py .../fixtures/does_not_exist
    known good  : python3 <run-tmp>/resources/audit_dir.py /tmp/tmp.yUr6uTrKiX   (recorded at @3)
```

`freeze` is the last step that succeeded before the incident. `known bad` is the argv that
failed. `known good` is stated **only** when a later run actually succeeded; where nothing
did, it says so instead of guessing. Rote's per-run temporary directories are collapsed to
`<run-tmp>` so the spec shows the real difference rather than a dead path.

Where nothing was recorded at all the spec says `no invocation recorded` rather than
printing an empty field.

The earliest failure in a workspace is marked as such. That is an ordering fact from the
recorded timestamps, and it is labelled as one: *"an ordering fact, not a cause."* It is
only printed where it distinguishes something. In a workspace with a single incident,
"earliest" and "only" are the same thing, and saying it there is noise.

## What it deliberately does not do

It does **not** name a root cause. *AI Agents in Depth* §7.5.2 describes failure
attribution and then says plainly:

> "An LLM can help with the work, but cannot replace the human, because failure
> attribution often surfaces product problems, not just technical ones."

Deciding which step *caused* a cascade is a judgement the recorded evidence does not
carry. What the evidence carries is: this command failed this many times, this later
command differed in these arguments and succeeded, that value is or is not written down,
and it cost this much. That is what gets reported.

## Was the lesson already written down?

With `repo=`, each repair value is searched in `AGENTS.md`, `CLAUDE.md`, `.cursorrules`,
`CONTRIBUTING.md` and `README.md`:

```
REPAIRED_RETRY  demo-wrong-directory @1 npm  — argv 1 -> 3, adding --prefix apps/api
  npm error Could not read package.json: ENOENT
  **not written down** in any instruction file that was checked
```

Whether a string appears in a file is a fact. Whether the agent *should* have known it is
not, so that is never asserted.

## Six things that were wrong first

**Pairing on request method.** Every process step in rote shares one method, `EXEC`, so
"a later EXEC succeeded" pairs completely unrelated commands.

**Rote's per-run temporary directories.** A play is unpacked into a fresh
`/tmp/.tmpXXXXXX` every run, so the same command never has byte-identical argv twice.
Before those are collapsed, *every* pair looks like a repair.

**Grouping hid the thing it was meant to show.** Once repeated failures were grouped into
one incident, three identical `pytest` attempts had no later failure to point at and came
out as `UNRECOVERED`. The repetition *is* the finding.

**A regression spec full of dead paths.** The first version normalised rote's per-run
temp directories when *comparing* argv but printed them raw, so the spec named a
`/tmp/.tmpIUxa3K` that no longer exists and nobody could act on.

**A feature the demo could not show.** Once the ordering marker was suppressed in
single-incident workspaces, it stopped appearing anywhere: neither the bundled
trajectories nor the real history on this machine had a workspace with two separate
incidents. Nor did anything demonstrate a repair that *was* already written down. Both
paths were correct and both were invisible, which for someone evaluating the play is the
same as absent. `demo-cascade` exists to exercise them.

**Payload size, and how this was got wrong twice.** Emitting every response produced
185 KB on one ordinary machine, past both argv limits and the 64 KiB ceiling on a step's
captured stdout. Emitting only the failures and their pairing candidates brought that to
9.7 KB — and that number came from a small history. On a machine with real history the
same code emitted **1,007,913 bytes**, and because the scan JSON is handed to the next step
as an argument it died at `Argument list too long` before the 64 KiB preview ever applied.
A reviewer running it on their own machine hit 2,392,794 bytes. Knowing a ceiling exists is
not the same as budgeting for it.

The second mistake was the first fix: capping the candidate list. Candidates were not the
cost, and dropping one can remove the later run that repaired a failure, turning
`REPAIRED_RETRY` into a false `UNRECOVERED`. **A size limit is not allowed to invent a
claim.** What actually costs bytes is a single argv element — an agent running a script
through `bash -lc` puts the whole script in argv, and one failure record measured 75,523
bytes by itself. So each element is shortened *for display* and marked, while identity is a
digest of the full argv, and the only candidates dropped are the ones no comparison can
reach. 27 KB across the same ten workspaces, with every verdict verified identical to the
previous version field by field. Where even that will not fit, whole workspaces are dropped
and **named in the report**.

## Licence

MIT
