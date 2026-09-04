# retry-ledger

**What did your agent fail at, what fixed it, and what did the detour cost?**

Rote records every step your agent took, including the ones that did not work. This reads
that record and reports the failure and repair structure it can prove.

```bash
rote play run https://play.modiqo.ai/rajdeepkushwaha/retry-ledger workspaces_root=auto
```

Zero credentials. Reads JSON only: it never executes anything and never modifies a
workspace. `python3`, nothing else.

## Verdicts

| verdict | what is being claimed |
|---|---|
| `UNCHANGED_RETRY` | The identical command was run again and failed again. |
| `REPAIRED_RETRY` | Something changed before it succeeded, and the argument that changed is named. |
| `UNRECOVERED` | It failed and no later run of the same program succeeded. |
| `TRANSIENT` | The same argv succeeded later. **Not proof the work was identical** — a script at the same path may have been rewritten in between. |
| `INDETERMINATE` | The attempts could not be paired. Not a claim that anything was fine. |

## What it deliberately does not do

It does **not** name a root cause. *AI Agents in Depth* §7.5.2 describes failure
attribution and then says plainly:

> "An LLM can help with the work, but cannot replace the human, because failure
> attribution often surfaces product problems, not just technical ones."

Deciding which step *caused* a cascade is a judgement the recorded evidence does not
carry. What the evidence does carry is: this command failed, this later command differed
in these arguments and succeeded, and it cost this much. That is what gets reported.

## Two things that were wrong first

**Pairing on request method.** Every process step in rote shares one method, `EXEC`, so
"a later EXEC succeeded" pairs completely unrelated commands. The first version did that
and produced confident nonsense. Attempts are paired on program and argv.

**Rote's per-run temporary directories.** A play is unpacked into a fresh `/tmp/.tmpXXXXXX`
on every run, so the same command never has byte-identical argv twice. Before those are
collapsed, *every* pair looks like a repair and the verdict means nothing.

## Scale

`scan` emits only the failed attempts and the later runs of the same program, never the
whole trajectory. Emitting everything produced a 185 KB payload on one ordinary machine,
which exceeds both argv limits and the 64 KiB ceiling on a step's captured stdout. The
compact form is 9.7 KB for the same history.

## Licence

MIT
