export type AgentEvent = {
  type?: string;
  run_id?: string;
  stage: string;
  agent: string;
  status: string;
  payload: Record<string, unknown>;
  error?: string;
  created_at?: string;
};

export type PipelineAgent = {
  name: string;
  mention: string;
  stage: string;
  kind: "band" | "infrastructure";
};

export type AgentStatus = "idle" | "active" | "complete" | "failed";

export type ThreadMessage = {
  id: string;
  kind: "system" | "handoff" | "active" | "complete" | "failed" | "terminal";
  agent: string;
  stage: string;
  status: string;
  title: string;
  body: string;
  mention?: string;
  fromAgent?: string;
  toAgent?: string;
  error?: string;
  payload?: Record<string, unknown>;
  timestamp?: string;
};

const DEFAULT_AGENTS: PipelineAgent[] = [
  { name: "Alert Triager", mention: "@alert-triager", stage: "triage", kind: "band" },
  {
    name: "Incident Reproducer",
    mention: "@incident-reproducer",
    stage: "repro",
    kind: "band",
  },
  { name: "Repro Sandbox", mention: "@repro-sandbox", stage: "repro", kind: "infrastructure" },
  {
    name: "Regression Test Generator",
    mention: "@test-generator",
    stage: "test",
    kind: "band",
  },
  { name: "Patch Generator", mention: "@patch-generator", stage: "fix", kind: "band" },
  {
    name: "Validation Swarm",
    mention: "@validation-swarm",
    stage: "validate",
    kind: "infrastructure",
  },
  { name: "RCA Publisher", mention: "@rca-publisher", stage: "rca", kind: "band" },
  { name: "Orchestrator", mention: "@orchestrator", stage: "orchestrator", kind: "infrastructure" },
];

export function getPipelineAgents(events: AgentEvent[]): PipelineAgent[] {
  const queued = events.find((e) => e.status === "queued");
  const roster = queued?.payload?.agents as PipelineAgent[] | undefined;
  return roster?.length ? roster : DEFAULT_AGENTS;
}

function severityLabel(sev?: string): string {
  const map: Record<string, string> = {
    sev1: "🔴 SEV-1 Critical",
    sev2: "🟠 SEV-2 High",
    sev3: "🟡 SEV-3 Medium",
    sev4: "🟢 SEV-4 Low",
  };
  return sev ? (map[sev] ?? sev) : "unknown severity";
}

function summarizeActive(event: AgentEvent): string {
  const p = event.payload as Record<string, unknown>;
  switch (event.stage) {
    case "triage":
      return "Reading the alert payload and classifying the incident by service, severity, and impacted components…";
    case "repro":
      if (event.agent === "Repro Sandbox") {
        const timeout = p?.timeout_seconds as number | undefined;
        return `Spinning up a Docker container${timeout ? ` (timeout: ${timeout}s)` : ""} to reproduce the failure from scratch…`;
      }
      return "Analysing the incident context and designing a deterministic repro plan to run in Docker…";
    case "test":
      return "Studying the Docker repro logs to write a strict regression test that fails on the bug and passes only after a correct fix…";
    case "fix":
      return "Reading the repro logs and regression test to generate two distinct patch candidates in unified-diff format…";
    case "validate":
      return "Running both patch candidates simultaneously in isolated Docker containers. First one to pass the regression test wins…";
    case "rca":
      return "Drafting the root cause analysis, commit message, and branch name using the winning patch and validation evidence…";
    default:
      return `${event.agent} is working on the ${event.stage} stage…`;
  }
}

function summarizeComplete(event: AgentEvent): string {
  const p = event.payload;
  switch (event.stage) {
    case "triage": {
      const ctx = p as { service?: string; severity?: string; error_signature?: string; environment?: string };
      return (
        `Triage complete for **${ctx.service ?? "unknown service"}** in ${ctx.environment ?? "unknown"} environment. ` +
        `Classified as ${severityLabel(ctx.severity)}. ` +
        `Error signature: "${ctx.error_signature ?? "unknown"}".`
      );
    }
    case "repro": {
      if (event.agent === "Repro Sandbox") {
        const exec = p as {
          exit_code?: number;
          failure_observed?: boolean;
          error?: string;
          command?: string;
          image?: string;
        };
        if (exec.error) {
          return `Docker repro encountered an error: ${exec.error}`;
        }
        const observed = exec.failure_observed ? "✅ confirmed" : "⚠️ not observed";
        return (
          `Repro sandbox finished. Image: \`${exec.image ?? "python:3.11-slim"}\`. ` +
          `Command exited with code ${exec.exit_code ?? "?"}. ` +
          `Failure ${observed}. Logs captured for test generation.`
        );
      }
      const plan = p as { steps?: string[]; expected_failure?: string };
      const stepCount = plan.steps?.length ?? 0;
      return (
        `Repro plan ready with ${stepCount} step${stepCount !== 1 ? "s" : ""}. ` +
        `Expected failure: "${plan.expected_failure ?? "as described in the alert"}". Handing off to Docker sandbox.`
      );
    }
    case "test": {
      const tests = p as { run_command?: string; test_files?: string[]; framework?: string };
      const files = tests.test_files?.join(", ") || "generated test file";
      return (
        `Regression test written using ${tests.framework ?? "pytest"}. ` +
        `File: \`${files}\`. ` +
        `Run with: \`${tests.run_command ?? "pytest"}\`.`
      );
    }
    case "fix": {
      const patches = p as { candidates?: { summary?: string }[] };
      const count = patches.candidates?.length ?? 0;
      const summaries = patches.candidates
        ?.map((c, i) => `  • Candidate ${i + 1}: ${c.summary ?? "patch"}`)
        .join("\n") ?? "";
      return (
        `Generated ${count} candidate patch${count !== 1 ? "es" : ""}:\n${summaries}\n` +
        `Sending both to the Validation Swarm for Docker testing.`
      );
    }
    case "validate": {
      const val = p as {
        winning_patch?: { summary?: string; files_changed?: string[] };
        winning_candidate_index?: number;
        results?: { candidate_index?: number; validation_passed?: boolean; error?: string }[];
      };
      if (val.winning_patch?.summary) {
        const idx = (val.winning_candidate_index ?? 0) + 1;
        const files = val.winning_patch.files_changed?.join(", ") ?? "changed files";
        return (
          `✅ Candidate #${idx} passed all tests: "${val.winning_patch.summary}".\n` +
          `Files changed: \`${files}\`. ` +
          `Losing container was stopped. Handing validated patch to RCA Publisher.`
        );
      }
      const failures = val.results
        ?.filter((r) => !r.validation_passed)
        .map((r) => `  • Candidate ${(r.candidate_index ?? 0) + 1}: ${r.error ?? "failed"}`)
        .join("\n") ?? "";
      return `❌ No candidate passed validation.\n${failures}`;
    }
    case "rca": {
      const rca = p as {
        title?: string;
        root_cause?: string;
        git_branch?: string;
        commit_message?: string;
      };
      return (
        `RCA published: "${rca.title ?? "report"}".\n` +
        `Root cause: ${rca.root_cause ?? "see report"}.\n` +
        `Branch: \`${rca.git_branch ?? "n/a"}\` | Commit: ${rca.commit_message ?? "n/a"}.`
      );
    }
    default:
      return `${event.agent} completed the ${event.stage} stage.`;
  }
}

function summarizeQueued(event: AgentEvent): string {
  const p = event.payload as Record<string, unknown>;
  const alert = p?.alert as Record<string, unknown> | undefined;
  const service = (alert?.service ?? alert?.service_short ?? "service") as string;
  const dockerOk = p?.docker_available as boolean | undefined;
  const dockerNote =
    dockerOk === true
      ? "Docker is ready."
      : dockerOk === false
        ? "⚠️ Docker is not available — repro and validation will be skipped."
        : "";
  return (
    `Incident pipeline started for **${service}**. ` +
    `Agent roster published with ${((p?.agents as unknown[]) ?? []).length} agents. ` +
    dockerNote
  ).trim();
}

export function buildThreadMessages(events: AgentEvent[]): ThreadMessage[] {
  const messages: ThreadMessage[] = [];

  for (const [index, event] of events.entries()) {
    const id = `${index}-${event.stage}-${event.status}`;
    const timestamp = event.created_at;

    if (event.type === "ping") continue;

    if (event.status === "queued") {
      messages.push({
        id,
        kind: "system",
        agent: event.agent,
        stage: event.stage,
        status: event.status,
        title: "Pipeline started",
        body: summarizeQueued(event),
        payload: event.payload,
        timestamp,
      });
      continue;
    }

    if (event.status === "handoff") {
      const fromAgent = (event.payload.from_agent as string) || event.agent;
      const toAgent = event.payload.to_agent as string;
      const mention = event.payload.mention as string | undefined;
      const summary =
        (event.payload.summary as string) ||
        `${fromAgent} finished its work and is passing the baton to ${toAgent}.`;
      messages.push({
        id,
        kind: "handoff",
        agent: event.agent,
        stage: event.stage,
        status: event.status,
        title: `${fromAgent} → ${toAgent}`,
        body: summary,
        mention,
        fromAgent,
        toAgent,
        payload: event.payload,
        timestamp,
      });
      continue;
    }

    if (event.status === "active") {
      messages.push({
        id,
        kind: "active",
        agent: event.agent,
        stage: event.stage,
        status: event.status,
        title: `${event.agent} is working`,
        body: summarizeActive(event),
        payload: event.payload,
        timestamp,
      });
      continue;
    }

    if (event.status === "complete") {
      messages.push({
        id,
        kind: "complete",
        agent: event.agent,
        stage: event.stage,
        status: event.status,
        title: `${event.agent} finished`,
        body: summarizeComplete(event),
        payload: event.payload,
        timestamp,
      });
      continue;
    }

    if (event.status === "failed") {
      const errorMsg = event.error || "Stage failed without a detailed message.";
      const errList = event.payload?.errors as string[] | undefined;
      const detail = errList?.length
        ? `\nDetails:\n${errList.map((e) => `  • ${e}`).join("\n")}`
        : "";
      messages.push({
        id,
        kind: "failed",
        agent: event.agent,
        stage: event.stage,
        status: event.status,
        title: `${event.agent} encountered a failure`,
        body: errorMsg + detail,
        error: event.error,
        payload: event.payload,
        timestamp,
      });
      continue;
    }

    if (event.status === "done") {
      const prUrl = event.payload.pr_url as string | undefined;
      const prError = event.payload.pr_error as string | undefined;
      const branch = event.payload.branch as string | undefined;
      const repoName = event.payload.repo_full_name as string | undefined;

      let body = "🎉 Pipeline complete! The validated fix is ready.";
      if (prUrl) {
        body = `🎉 Fix merged to branch \`${branch ?? "fix-branch"}\` on **${repoName ?? "repo"}**. Pull request is open and ready for review.`;
      } else if (branch) {
        body = `✅ Fix validated. Branch \`${branch}\` is ready to push${repoName ? ` to ${repoName}` : ""}. Open the Final Report tab to apply it.`;
      } else if (prError) {
        body = `✅ Fix validated locally. PR could not be opened automatically: ${prError}. See the Final Report tab for manual steps.`;
      }

      messages.push({
        id,
        kind: "terminal",
        agent: event.agent,
        stage: event.stage,
        status: event.status,
        title: "Pipeline complete",
        body,
        payload: event.payload,
        timestamp,
      });
    }
  }

  return messages;
}

export function getAgentStatuses(
  agents: PipelineAgent[],
  events: AgentEvent[],
): Record<string, AgentStatus> {
  const statuses: Record<string, AgentStatus> = {};
  for (const agent of agents) {
    statuses[agent.name] = "idle";
  }

  for (const event of events) {
    const names = [event.agent];
    if (event.status === "handoff") {
      const to = event.payload.to_agent as string | undefined;
      if (to) names.push(to);
    }

    for (const name of names) {
      if (!(name in statuses)) continue;
      if (event.status === "active") statuses[name] = "active";
      if (event.status === "complete" || event.status === "done") {
        statuses[name] = "complete";
      }
      if (event.status === "failed") statuses[name] = "failed";
    }
  }

  const lastActive = [...events].reverse().find((e) => e.status === "active");
  if (lastActive && statuses[lastActive.agent] !== "failed") {
    statuses[lastActive.agent] = "active";
  }

  return statuses;
}
