#!/usr/bin/env -S rote play run
/**
 * retry-ledger
 *
 * What did your agent fail at, what fixed it, and what did the detour cost?
 *
 * @rote-frontmatter
 * ---
 * name: retry-ledger
 * source_url: https://github.com/RajdeepKushwaha5/retry-ledger
 * tags:
 * - agent-ops
 * - observability
 * - rote
 * - debugging
 * - effect-read-only
 * output:
 *   format: json
 * description: Rote already records every step your agent took, including the ones that failed. This reads that record and tells you what went wrong, what fixed it, and what the detour cost in time and tokens. If the same command failed five times it is shown as one problem, not five. UNCHANGED_RETRY means the identical command was run again and failed again. REPAIRED_RETRY means something changed before it worked, and it tells you exactly which argument changed and whether that fix is already written down in your AGENTS.md or CLAUDE.md. TRANSIENT means the same command worked later on its own. UNRECOVERED means it never worked. INDETERMINATE means the attempts could not be matched up, which is not a claim that everything was fine. It does not tell you which step caused the trouble, because the recording does not show that and guessing would be worse than useless. It does tell you which failure came first, and for each problem it shows the last thing that worked before it, the command that failed, and the command that worked afterwards. Pass workspaces_root=demo to try it with nothing set up. Before it reads your history it runs its own test cases through the same analyzer and prints the result. If those fail it shows you that instead of findings. It reads files only. Read-only, no credentials, no network. Needs python3.
 * provenance:
 *   author: rajdeepkushwaha
 * parameters:
 * - name: workspaces_root
 *   param_type: string
 *   required: false
 *   default: demo
 *   description: Directory holding rote workspaces. "auto" resolves the standard location under your home directory; "demo" runs the bundled trajectories with no setup.
 * - name: workspace
 *   param_type: string
 *   required: false
 *   default: ''
 *   description: Optional substring; limits the report to matching workspace names.
 * - name: repo
 *   param_type: string
 *   required: false
 *   default: ''
 *   description: Optional repository whose AGENTS.md, CLAUDE.md and .cursorrules are searched for each repair value. "demo" uses the bundled one.
 * metadata:
 *   rote_version: 0.79.0
 *   version: 0.5.2
 *   status: released
 *   kind: atomic
 *   flow_type: sequential
 *   execution_model: steps_with_presentation
 *   format: typescript
 *   requires_sessions: false
 * presentation_fixtures:
 *   selfcheck: resources/presentation-fixtures/selfcheck/fixture.yaml
 *   scan: resources/presentation-fixtures/scan/fixture.yaml
 *   classify: resources/presentation-fixtures/classify/fixture.yaml
 * steps:
 *   selfcheck:
 *     type: process.exec
 *     timeout_ms: 120000
 *     argv:
 *     - python3
 *     - '@resource{selfcheck.py}'
 *   scan:
 *     type: process.exec
 *     timeout_ms: 120000
 *     argv:
 *     - python3
 *     - '@resource{ledger.py}'
 *     - scan
 *     - $workspaces_root
 *     - $workspace
 *     - $repo
 *   classify:
 *     type: process.exec
 *     timeout_ms: 120000
 *     depends_on:
 *     - scan
 *     argv:
 *     - python3
 *     - '@resource{ledger.py}'
 *     - classify
 *     - $workspaces_root
 *     - '@scan{.stdout.text}'
 * ---
 */

const { FlowOutput, loadPresentationContext, stepName } =
  await import("__ROTE_PRESENTATION_SDK__");

const out = new FlowOutput();
const ctx = await loadPresentationContext();

type Incident = {
  workspace: string;
  first_response: number;
  attempts: number;
  responses: number[];
  program: string | null;
  argv: string[];
  status: number;
  duration_ms: number;
  tokens: number;
  stderr: string;
  verdict: string;
  detail: string;
  paired_with: number | null;
  repair_value: string | null;
  documented: string;
  documented_in: string;
  is_first_failure_in_workspace: boolean;
  regression: {
    freeze_after: number | null;
    known_bad: string[];
    known_good: string[] | null;
    known_good_recorded_at: number | null;
    note: string;
  };
};

// Every step is inspected, not just the one carrying the payload. A step that was
// blocked, skipped, failed or cut at rote's 64 KiB stdout preview must be named:
// a report built from a partial document is not a shorter report, it is a wrong one.
type Degraded = { step: string; state: string; detail: string };
const degraded: Degraded[] = [];

// Takes the handle, not the name, so every stepName("...") stays a literal lint can verify.
function checkStep(name: string, step: ReturnType<typeof ctx.step>) {
  const o = step.outcome as { status: string; output?: Record<string, unknown> };
  if (o.status !== "completed" && o.status !== "restored") {
    degraded.push({
      step: name,
      state: o.status,
      detail: String(o.output?.reason ?? o.output?.message ?? "no detail recorded"),
    });
    return null;
  }
  return (o.output ?? {}) as { body?: Record<string, unknown> };
}

const selfStep = checkStep("selfcheck", ctx.step(stepName("selfcheck")));
const scanStep = checkStep("scan", ctx.step(stepName("scan")));
const classStep = checkStep("classify", ctx.step(stepName("classify")));

for (const [name, st] of [["selfcheck", selfStep], ["scan", scanStep], ["classify", classStep]] as const) {
  if (!st) continue;
  const body = (st.body ?? {}) as { stdout?: { truncated?: boolean; bytes?: number } };
  if (body.stdout?.truncated === true) {
    degraded.push({
      step: name,
      state: "truncated",
      detail: `stdout was cut at rote's 64 KiB preview ceiling (${body.stdout?.bytes ?? "?"} bytes produced)`,
    });
  }
}

if (degraded.length > 0) {
  const rows = degraded.map((d) => `- ${d.step}: ${d.state} — ${d.detail}`).join("\n");
  out.human(
    `# Ledger incomplete\n\nThis run did not produce a usable report, so nothing is shown. Reading a partial trajectory as a clean one is the exact mistake this play exists to catch.\n\n${rows}`,
  );
  out.summary(`incomplete: ${degraded.map((d) => `${d.step} ${d.state}`).join(", ")}`);
  out.result({ status: "incomplete", degraded, incidents: [] });
} else {
  const body = (classStep!.body ?? {}) as { stdout?: { text?: string } };
  const stdout = body.stdout?.text;
  if (stdout === undefined) throw new Error("classify captured no stdout");

  let incidents: Incident[] = [];
  let totals = { incidents: 0, attempts: 0, wasted_ms: 0, wasted_tokens: 0 };
  let scanned = 0;
  // Held at this scope so the heading can use it. `scanned` is a NUMBER, and reading
  // .root_label off a number yields undefined in silence rather than raising, which is
  // how the first version of this looked correct and did nothing.
  let rootLabel = "";
  let wsCount = 0;
  let firstFailures: Record<string, number | null> = {};
  let checked: string[] = [];
  let unreadable: { path: string; reason: string }[] = [];
  let truncatedWs = false;
  let omittedWs: string[] = [];
  try {
    const parsed = JSON.parse(stdout) as {
      workspaces_with_failures: number;
      first_failures: Record<string, number | null>;
      responses_scanned: number;
      instructions_checked: string[];
      unreadable: { path: string; reason: string }[];
      truncated_workspaces: boolean;
      omitted_workspaces: string[];
      root_label?: string;
      totals: { incidents: number; attempts: number; wasted_ms: number; wasted_tokens: number };
      incidents: Incident[];
    };
    incidents = parsed.incidents;
    totals = parsed.totals;
    scanned = parsed.responses_scanned;
    rootLabel = parsed.root_label ?? "";
    wsCount = parsed.workspaces_with_failures;
    firstFailures = parsed.first_failures ?? {};
    checked = parsed.instructions_checked ?? [];
    unreadable = parsed.unreadable ?? [];
    truncatedWs = parsed.truncated_workspaces;
    omittedWs = parsed.omitted_workspaces ?? [];
  } catch (cause) {
    throw new Error("classify stdout was not valid JSON", { cause });
  }

  const order = ["UNCHANGED_RETRY", "REPAIRED_RETRY", "UNRECOVERED", "TRANSIENT", "INDETERMINATE"];
  const blurb: Record<string, string> = {
    UNCHANGED_RETRY: "The identical command was run again and failed again. Nothing changed between attempts.",
    REPAIRED_RETRY: "Something changed before it succeeded. The argument that changed is named, and so is whether it is already written down.",
    UNRECOVERED: "It failed and no later run of the same program succeeded.",
    TRANSIENT: "The same argv succeeded later. Argv equality is not proof the work was identical: a script at the same path may have been rewritten in between.",
    INDETERMINATE: "The attempts could not be paired. Not a claim that anything was fine.",
  };

  const perWorkspace: Record<string, number> = {};
  for (const i of incidents) perWorkspace[i.workspace] = (perWorkspace[i.workspace] ?? 0) + 1;

  function row(i: Incident): string {
    const times = i.attempts > 1 ? ` ×${i.attempts}` : "";
    const paired = i.paired_with === null ? "" : ` (paired with @${i.paired_with})`;
    const secs = (i.duration_ms / 1000).toFixed(1);
    let doc = "";
    if (i.verdict === "REPAIRED_RETRY") {
      if (i.documented === "documented") doc = `\n  already written down in ${i.documented_in}`;
      else if (i.documented === "undocumented") doc = `\n  **not written down** in any instruction file that was checked`;
      else if (i.documented === "not-checked") doc = `\n  no repo supplied, so nothing was checked`;
      // the value was shortened to fit, so searching for it would test the shortening
      else if (i.documented === "value-elided") doc = `\n  the repair value was too long to carry in full, so it was not searched for`;
    }
    const err = i.stderr ? `\n  ${i.stderr.split("\n")[0].slice(0, 150)}` : "";
    const first = i.is_first_failure_in_workspace && perWorkspace[i.workspace] > 1
      ? "\n  earliest recorded failure in this workspace (an ordering fact, not a cause)"
      : "";
    const r = i.regression;
    const freeze = r.freeze_after === null
      ? "nothing succeeded before it"
      : `after @${r.freeze_after}`;
    const good = r.known_good === null
      ? `      known good  : none recorded — ${r.note}`
      : `      known good  : ${r.known_good.join(" ")}   (recorded at @${r.known_good_recorded_at})`;
    const reg = [
      "\n  regression prefix:",
      `      freeze      : ${freeze}`,
      `      known bad   : ${r.known_bad.filter((x) => x).join(" ") || "no invocation recorded"}`,
      good,
    ].join("\n");
    return `- \`${i.workspace}\` @${i.first_response}${times} ${i.program ?? "(no program recorded)"}${paired} — ${i.detail} · ${secs}s, ${i.tokens} tokens${err}${doc}${first}${reg}`;
  }

  const sections: string[] = [];
  for (const v of order) {
    const rows = incidents.filter((i) => i.verdict === v);
    if (rows.length === 0) continue;
    sections.push(`## ${v} (${rows.length})\n${blurb[v]}\n${rows.map(row).join("\n")}`);
  }
  if (incidents.length === 0) {
    sections.push(
      `## No failures recorded\nAcross ${scanned} response(s) rote recorded no failed step. That is what the evidence says, not a claim that nothing ever went wrong. Run with \`workspaces_root=demo\` to see the taxonomy on bundled trajectories.`,
    );
  }
  sections.push(
    checked.length > 0
      ? `## Instruction files checked\n${checked.map((c) => `- ${c}`).join("\n")}\nA repair counts as written down only when the value that fixed it literally appears in one of these.`
      : `## Instruction files checked\nNone. Pass \`repo=\` to have each repair value searched in AGENTS.md, CLAUDE.md and .cursorrules.`,
  );
  if (unreadable.length > 0) {
    sections.push(
      `## Incomplete scan (${unreadable.length})\nThese were not read, so a short ledger does not mean a clean history.\n${unreadable.map((u) => `- ${u.path} (${u.reason})`).join("\n")}`,
    );
  }
  if (truncatedWs) {
    sections.push(`## Truncated\nMore workspaces exist than were scanned; this ledger is partial.`);
  }
  if (omittedWs.length > 0) {
    sections.push(
      `## Left out to fit (${omittedWs.length})\nA step's captured output is previewed at 64 KiB, and these workspaces did not fit inside it. They were read; they are not reported. This is a limit of this play, not a quiet history.\n${
        omittedWs.map((w) => `- ${w}`).join("\n")
      }`,
    );
  }

  // The assertions that prove this analyzer works live on the author's machine,
  // where nobody running the play can see them. This is that same analyzer, fed its
  // own bundled trajectories at run time. A rule with no positive case could be
  // deleted without the check noticing, so coverage of every verdict is itself a
  // case, and so is discovery: a scan that reads nothing reports a clean history.
  type SelfCheck = { passed: number; total: number; failures: { case: string; detail: string }[] };
  let selfResult: SelfCheck | null = null;
  if (selfStep) {
    const sb = (selfStep.body ?? {}) as { stdout?: { text?: string } };
    try { selfResult = JSON.parse(sb.stdout?.text ?? '') as SelfCheck; } catch { selfResult = null; }
  }
  const selfFailed = selfResult === null || selfResult.failures.length > 0;
  const selfLine = selfResult === null
    ? 'Self-check: NOT RUN - this analyzer did not verify itself on this machine'
    : selfResult.failures.length === 0
      ? `Self-check: PASSED (${selfResult.passed}/${selfResult.total} bundled analyzer cases)`
      : `Self-check: FAILED (${selfResult.passed}/${selfResult.total}) - THIS ANALYZER IS NOT BEHAVING AS BUILT`;

  const secs = (totals.wasted_ms / 1000).toFixed(1);
  if (selfFailed) {
    const failRows = (selfResult?.failures ?? [])
      .map((f) => `  failed case  ${f.case}: ${f.detail}`).join('\n');
    out.human([selfLine,
      '# ANALYZER FAILED ITS OWN SELF-CHECK - THE FINDINGS BELOW CANNOT BE RELIED ON',
      'This is the same analyzer that would have read your trajectories. It was given its own bundled ones first and did not reproduce them, so nothing it reports about your history is trustworthy.',
      failRows].join('\n\n'));
    out.summary(selfLine);
    out.result({ self_check: selfResult, status: 'self-check-failed' });
  } else {
  out.human(
    [selfLine,
     `# Retry ledger${
       rootLabel.startsWith("the bundled") ? " for the bundled example" : ""
     }: ${totals.incidents} incident(s) from ${totals.attempts} failed attempt(s) across ${wsCount} workspace(s)`,
     `Those attempts cost **${secs}s** and **${totals.wasted_tokens} tokens** before anything worked.`,
     ...sections].join("\n\n"),
  );
  out.summary(
    `${totals.incidents} incident(s), ${totals.attempts} failed attempt(s) in ${scanned} response(s): ${secs}s and ${totals.wasted_tokens} tokens spent on work that did not succeed`,
  );
  out.result({
    self_check: selfResult,
    // three views of one run. The listing is capped in the
    // human view, so which view is canonical is stated here
    // rather than left for a reader to discover.
    representations: {
      human: "complete: every incident with its verdict, cost, regression prefix and whether the repair is written down",
      json: "canonical superset: the same incidents plus the self-check result, the first-failure ordering and which instruction files were checked",
      summary: "intentionally lossy: incident and attempt counts with the time and tokens spent",
    },
    first_failures: firstFailures,
    responses_scanned: scanned,
    workspaces_with_failures: wsCount,
    instructions_checked: checked,
    totals,
    unreadable,
    truncated_workspaces: truncatedWs,
    incidents,
  });
  }
}
