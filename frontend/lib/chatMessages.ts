import { AgentEvent } from "./agentThread";
import { buildThreadMessages, ThreadMessage } from "./agentThread";

export type ChatMessage = {
  id: string;
  variant: "system" | "agent" | "handoff" | "success" | "error";
  author: string;
  initials: string;
  text: string;
  time?: string;
  stage?: string;
};

const AGENT_COLORS: Record<string, string> = {
  Orchestrator: "#64748b",
  "Alert Triager": "#6366f1",
  "Incident Reproducer": "#8b5cf6",
  "Repro Sandbox": "#0ea5e9",
  "Regression Test Generator": "#14b8a6",
  "Patch Generator": "#f59e0b",
  "Validation Swarm": "#06b6d4",
  "RCA Publisher": "#10b981",
};

export function agentAccentColor(name: string): string {
  if (AGENT_COLORS[name]) return AGENT_COLORS[name];
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) {
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
  }
  const hue = Math.abs(hash) % 360;
  return `hsl(${hue} 55% 45%)`;
}

function initialsFor(name: string): string {
  const parts = name.split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function formatTime(iso?: string): string | undefined {
  if (!iso) return undefined;
  try {
    return new Date(iso).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return undefined;
  }
}

function threadToChat(message: ThreadMessage): ChatMessage {
  const author =
    message.kind === "handoff"
      ? (message.fromAgent ?? message.agent)
      : message.agent;

  let variant: ChatMessage["variant"] = "agent";
  let text = message.body;

  switch (message.kind) {
    case "system":
      variant = "system";
      text = "Started the incident pipeline and assigned the agent roster.";
      break;
    case "handoff":
      variant = "handoff";
      text = message.toAgent
        ? `Passing this to ${message.toAgent}. ${message.body}`
        : message.body;
      break;
    case "active":
      text =
        message.stage === "repro"
          ? "Setting up the Docker sandbox to reproduce the failure…"
          : message.stage === "validate"
            ? "Running candidate patches through the validation swarm…"
            : message.stage === "fix"
              ? "Generating patch candidates…"
              : message.stage === "test"
                ? "Writing a regression test…"
                : message.stage === "rca"
                  ? "Drafting the root cause analysis…"
                  : `Working on ${message.stage}…`;
      break;
    case "complete":
      text = message.body;
      break;
    case "failed":
      variant = "error";
      text = message.error ?? message.body;
      break;
    case "terminal":
      variant = "success";
      text = message.body;
      break;
    default:
      break;
  }

  return {
    id: message.id,
    variant,
    author,
    initials: initialsFor(author),
    text,
    time: formatTime(message.timestamp),
    stage: message.stage,
  };
}

export function buildChatMessages(events: AgentEvent[]): ChatMessage[] {
  return buildThreadMessages(events).map(threadToChat);
}
