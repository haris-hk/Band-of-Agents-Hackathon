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

function summarizeComplete(event: AgentEvent): string {
  const p = event.payload;
  switch (event.stage) {
    case "triage": {
      const ctx = p as { service?: string; severity?: string; error_signature?: string };
      return `Structured context for ${ctx.service ?? "service"} (${ctx.severity ?? "unknown"}): ${ctx.error_signature ?? "error"}`;
    }
    case "repro":
      if (event.agent === "Repro Sandbox") {
        const exec = p as {
          exit_code?: number;
          failure_observed?: boolean;
          error?: string;
        };
        if (exec.error) return `Docker repro failed: ${exec.error}`;
        return `Docker repro exit ${exec.exit_code ?? "?"}; failure observed: ${exec.failure_observed ? "yes" : "no"}`;
      }
      return "Reproduction plan ready for sandbox execution.";
    case "test": {
      const tests = p as { run_command?: string; test_files?: string[] };
      const files = tests.test_files?.join(", ") || "generated test";
      return `Regression test at ${files}; run: ${tests.run_command ?? "pytest"}`;
    }
    case "fix": {
      const patches = p as { candidates?: { summary?: string }[] };
      const count = patches.candidates?.length ?? 0;
      return `Generated ${count} candidate patch(es) for validation.`;
    }
    case "validate": {
      const val = p as {
        winning_patch?: { summary?: string };
        winning_candidate_index?: number;
      };
      if (val.winning_patch?.summary) {
        return `Winner: candidate #${val.winning_candidate_index ?? 0} — ${val.winning_patch.summary}`;
      }
      return "Validation swarm finished evaluating candidates.";
    }
    case "rca": {
      const rca = p as { title?: string; root_cause?: string };
      return `RCA published: ${rca.title ?? "report"} — ${rca.root_cause ?? ""}`.trim();
    }
    default:
      return `${event.agent} completed ${event.stage} stage.`;
  }
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
        title: "Pipeline queued",
        body: "Orchestrator accepted the incident and published the agent roster.",
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
        `${fromAgent} handed work to ${toAgent}.`;
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
        body: `Stage: ${event.stage}`,
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
      messages.push({
        id,
        kind: "failed",
        agent: event.agent,
        stage: event.stage,
        status: event.status,
        title: `${event.agent} failed`,
        body: event.error || "Stage failed without a detailed message.",
        error: event.error,
        payload: event.payload,
        timestamp,
      });
      continue;
    }

    if (event.status === "done") {
      const prUrl = event.payload.pr_url as string | undefined;
      messages.push({
        id,
        kind: "terminal",
        agent: event.agent,
        stage: event.stage,
        status: event.status,
        title: "Pipeline complete",
        body: prUrl
          ? `Validated fix pushed; pull request ready.`
          : "Pipeline finished. See RCA and fix artifacts below.",
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
