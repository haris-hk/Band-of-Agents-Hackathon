"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AgentChat } from "../components/AgentChat";
import { ChangesTab } from "../components/ChangesTab";
import { InputTab } from "../components/InputTab";
import { ReportTab } from "../components/ReportTab";
import { FixExportPayload, WorkspaceTab } from "../components/types";
import {
  AgentEvent,
  getAgentStatuses,
  getPipelineAgents,
} from "../lib/agentThread";
import { buildChatMessages } from "../lib/chatMessages";
import { parseUnifiedDiff } from "../lib/parseDiff";

type PipelineStep = { stage: string; label: string };

type HealthState = {
  loading: boolean;
  dockerAvailable: boolean | null;
  dockerSmokeOk: boolean | null;
  dockerMessage: string | null;
  dockerRemediation: string | null;
  backendOk: boolean;
  error: string | null;
};

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const INCIDENT_API_KEY = process.env.NEXT_PUBLIC_INCIDENT_API_KEY ?? "";

function buildWsUrl(): string {
  const base = API_BASE.replace(/^http/, "ws") + "/ws/incidents";
  if (!INCIDENT_API_KEY) return base;
  return `${base}?api_key=${encodeURIComponent(INCIDENT_API_KEY)}`;
}

function stepStatus(
  stage: string,
  events: AgentEvent[],
  terminal: "running" | "done" | "failed" | "idle",
): "pending" | "active" | "complete" | "failed" {
  const stageEvents = events.filter((e) => e.stage === stage);
  if (stageEvents.some((e) => e.status === "failed")) return "failed";
  if (stageEvents.some((e) => e.status === "complete" || e.status === "done"))
    return "complete";
  if (stageEvents.some((e) => e.status === "active")) return "active";
  if (terminal === "failed" && stage === "failed") return "failed";
  if (terminal === "done" && stage === "done") return "complete";
  return "pending";
}

const WORKSPACE_TAB_ORDER: WorkspaceTab[] = [
  "input",
  "chat",
  "changes",
  "report",
];

const TAB_LABELS: Record<WorkspaceTab, string> = {
  input: "Incident input",
  chat: "Agent chat",
  changes: "Code changes",
  report: "Final report",
};

export default function IncidentDashboard() {
  const [activeTab, setActiveTab] = useState<WorkspaceTab>("input");
  const [mode, setMode] = useState<"demo" | "github">("demo");
  const [repoUrl, setRepoUrl] = useState("https://github.com/org/repo");
  const [errorText, setErrorText] = useState(
    "TypeError: cannot read property customer_id of null",
  );
  const [impact, setImpact] = useState("Checkout failures for paid traffic");
  const [githubToken, setGithubToken] = useState("");
  const [demoAlert, setDemoAlert] = useState<Record<string, unknown> | null>(null);
  const [submittedAlert, setSubmittedAlert] = useState<Record<string, unknown> | null>(
    null,
  );
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [running, setRunning] = useState(false);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [showDebug, setShowDebug] = useState(false);
  const [health, setHealth] = useState<HealthState>({
    loading: true,
    dockerAvailable: null,
    dockerSmokeOk: null,
    dockerMessage: null,
    dockerRemediation: null,
    backendOk: false,
    error: null,
  });
  const wsRef = useRef<WebSocket | null>(null);

  const refreshHealth = useCallback(async () => {
    setHealth((current) => ({ ...current, loading: true, error: null }));
    try {
      const response = await fetch(`${API_BASE}/health`, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`Health check failed (${response.status})`);
      }
      const data = (await response.json()) as {
        docker_available?: boolean;
        docker_smoke_ok?: boolean | null;
        docker_message?: string | null;
        docker_remediation?: string | null;
      };
      setHealth({
        loading: false,
        backendOk: true,
        dockerAvailable: data.docker_available ?? null,
        dockerSmokeOk: data.docker_smoke_ok ?? null,
        dockerMessage: data.docker_message ?? null,
        dockerRemediation: data.docker_remediation ?? null,
        error: null,
      });
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "Cannot reach backend API";
      setHealth({
        loading: false,
        backendOk: false,
        dockerAvailable: null,
        dockerSmokeOk: null,
        dockerMessage: null,
        dockerRemediation: null,
        error: message,
      });
    }
  }, []);

  // Initial fetch + periodic health poll every 30 s
  useEffect(() => {
    void refreshHealth();
    const interval = setInterval(() => {
      void refreshHealth();
    }, 30_000);
    return () => clearInterval(interval);
  }, [refreshHealth]);

  // Pre-fetch the demo alert payload once on mount
  useEffect(() => {
    void fetch(`${API_BASE}/demo/alert`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data && typeof data === "object") setDemoAlert(data as Record<string, unknown>);
      })
      .catch(() => {
        /* optional until backend is up */
      });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const scopedEvents = useMemo(() => {
    if (!runId) return events;
    return events.filter((event) => {
      if (event.run_id) return event.run_id === runId;
      const payloadRunId = event.payload?.run_id as string | undefined;
      return payloadRunId === runId;
    });
  }, [events, runId]);

  const agents = useMemo(() => getPipelineAgents(scopedEvents), [scopedEvents]);
  const agentStatuses = useMemo(
    () => getAgentStatuses(agents, scopedEvents),
    [agents, scopedEvents],
  );
  const chatMessages = useMemo(
    () => buildChatMessages(scopedEvents),
    [scopedEvents],
  );

  const dockerStatus = useMemo(() => {
    const queued = scopedEvents.find((e) => e.status === "queued");
    return {
      available: queued?.payload?.docker_available as boolean | undefined,
      message: queued?.payload?.docker_message as string | undefined,
    };
  }, [scopedEvents]);

  const pipeline = useMemo(() => {
    const queued = scopedEvents.find((e) => e.status === "queued");
    return (queued?.payload?.pipeline as PipelineStep[] | undefined) ?? [];
  }, [scopedEvents]);

  const terminal = useMemo((): "running" | "done" | "failed" | "idle" => {
    if (scopedEvents.some((e) => e.stage === "failed" && e.status === "failed"))
      return "failed";
    if (scopedEvents.some((e) => e.status === "done")) return "done";
    if (running) return "running";
    return "idle";
  }, [scopedEvents, running]);

  const final = useMemo(() => {
    const done = [...scopedEvents]
      .reverse()
      .find((event) => event.status === "done");
    const failed = [...scopedEvents]
      .reverse()
      .find((event) => event.stage === "failed" && event.status === "failed");
    const payload = done?.payload ?? failed?.payload ?? {};
    const fixObj = payload.fix as { patch_unified_diff?: string } | undefined;
    const testsObj = payload.tests as { test_code?: string; run_command?: string } | undefined;
    const fixExport = payload.fix_export as FixExportPayload | undefined;
    const patch =
      fixObj?.patch_unified_diff ?? fixExport?.patch_unified_diff ?? "";
    return {
      rca:
        (payload.rca as { final_markdown?: string } | undefined)?.final_markdown ??
        "",
      fix: patch,
      testCode: testsObj?.test_code ?? fixExport?.test_code ?? "",
      testCommand:
        testsObj?.run_command ?? fixExport?.test_command ?? "",
      fixExport: fixExport ?? null,
      prUrl: (payload.pr_url as string | undefined) ?? "",
      prError: (payload.pr_error as string | undefined) ?? "",
      branch:
        (payload.branch as string | undefined) ?? fixExport?.branch_name ?? "",
      repoName: (payload.repo_full_name as string | undefined) ?? "",
      errors:
        (failed?.payload?.errors as string[] | undefined) ??
        (done?.payload?.errors as string[] | undefined) ??
        [],
      failureMessage: failed?.error ?? "",
    };
  }, [scopedEvents]);

  const parsedDiff = useMemo(() => parseUnifiedDiff(final.fix), [final.fix]);

  const dockerReady =
    health.dockerAvailable === true ||
    (health.dockerAvailable === null && dockerStatus.available !== false);

  const canRun =
    !running &&
    health.backendOk &&
    dockerReady &&
    (mode === "demo" ? demoAlert !== null : repoUrl.trim().length > 0);

  function buildAlert(): Record<string, unknown> {
    if (mode === "demo" && demoAlert) {
      return { ...demoAlert, source: "web-ui-demo" };
    }
    const alert: Record<string, unknown> = {
      repo_url: repoUrl.trim(),
      error: errorText.trim(),
      impact: impact.trim(),
      source: "web-ui",
    };
    if (githubToken.trim()) {
      alert.github_token = githubToken.trim();
    }
    return alert;
  }

  function submitViaWebSocket(ws: WebSocket) {
    const alert = buildAlert();
    setSubmittedAlert(alert);
    setConnectionError(null);
    setRunning(true);
    setEvents([]);
    setRunId(null);
    setActiveTab("chat");
    ws.send(JSON.stringify({ alert }));
  }

  /** POST /incidents as an alternative submission path (used when WS is unavailable). */
  async function submitViaHttp(alert: Record<string, unknown>): Promise<void> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (INCIDENT_API_KEY) headers["X-API-Key"] = INCIDENT_API_KEY;
    const response = await fetch(`${API_BASE}/incidents`, {
      method: "POST",
      headers,
      body: JSON.stringify({ alert }),
    });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(`POST /incidents failed (${response.status}): ${text}`);
    }
  }

  function runPipeline() {
    if (!health.backendOk) {
      setConnectionError(
        health.error ?? "Backend is not reachable. Start uvicorn on port 8000.",
      );
      return;
    }
    if (health.dockerAvailable === false) {
      setConnectionError(
        health.dockerRemediation ??
          health.dockerMessage ??
          "Docker is not running. Start Docker Desktop first.",
      );
      return;
    }

    let ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      submitViaWebSocket(ws);
      return;
    }

    ws = new WebSocket(buildWsUrl());
    wsRef.current = ws;
    setEvents([]);
    setRunId(null);
    setRunning(true);
    setConnectionError(null);
    setActiveTab("chat");

    ws.onopen = () => {
      setConnected(true);
      // Primary path: send the alert over the same WebSocket connection.
      // The backend starts the pipeline and streams AgentEvents back over this socket.
      submitViaWebSocket(ws!);
    };
    ws.onmessage = (message) => {
      let event: AgentEvent;
      try {
        event = JSON.parse(message.data as string) as AgentEvent;
      } catch {
        // Ignore non-JSON frames (should not happen with well-behaved backend)
        return;
      }
      // Handle heartbeat ping — respond with pong and ignore
      if (event.type === "ping") {
        ws?.send(JSON.stringify({ type: "pong" }));
        return;
      }
      setEvents((current) => [...current, event]);
      // Capture the run_id from the event itself or from the queued payload
      if (event.run_id) {
        setRunId(event.run_id);
      } else if (event.status === "queued") {
        const id = event.payload?.run_id as string | undefined;
        if (id) setRunId(id);
      }
      if (event.status === "done") {
        setRunning(false);
        setActiveTab("report");
      }
      if (event.status === "failed") {
        setRunning(false);
        if (event.payload?.fix) {
          setActiveTab("changes");
        } else {
          setActiveTab("report");
        }
      }
    };
    ws.onclose = (ev) => {
      setConnected(false);
      setRunning(false);
      if (ev.code === 4401) {
        setConnectionError(
          "WebSocket rejected: set NEXT_PUBLIC_INCIDENT_API_KEY to match INCIDENT_API_KEY.",
        );
      }
    };
    ws.onerror = () => {
      setConnected(false);
      // Fallback: if the WS can't connect, submit via POST /incidents so the pipeline
      // still starts on the backend. The user won't get live streaming but the run starts.
      const alert = buildAlert();
      void submitViaHttp(alert)
        .then(() => {
          // Pipeline started via HTTP; inform the user the WS streaming is unavailable.
          setConnectionError(
            `WebSocket unavailable — pipeline submitted via HTTP. Live event stream is offline. Check backend logs.`,
          );
          setRunning(false);
        })
        .catch((err: unknown) => {
          const msg = err instanceof Error ? err.message : String(err);
          setRunning(false);
          setConnectionError(
            `Cannot connect to ${buildWsUrl()} and HTTP fallback also failed: ${msg}`,
          );
        });
    };
  }

  const displayPipeline =
    pipeline.length > 0
      ? pipeline
      : [
          { stage: "triage", label: "Triage" },
          { stage: "repro", label: "Reproduce" },
          { stage: "test", label: "Test" },
          { stage: "fix", label: "Fix" },
          { stage: "validate", label: "Validate" },
          { stage: "rca", label: "RCA" },
        ];

  const pushStepLabel =
    terminal === "done"
      ? final.prUrl
        ? "PR opened"
        : final.prError
          ? "Done (no PR)"
          : "Complete"
      : "Push / PR";

  const tabBadges: Partial<Record<WorkspaceTab, number | boolean>> = {
    chat: chatMessages.length > 0 ? chatMessages.length : false,
    report: Boolean(final.rca || terminal !== "idle"),
    changes: Boolean(final.fix || final.testCode),
  };

  return (
    <main className="appShell">
      <header className="appHeader">
        <div className="appHeaderMain">
          <h1>Incident Console</h1>
          <span className={`statusBadge ${connected ? "live" : ""}`}>
            {connected ? "Live" : "Offline"}
          </span>
          {runId ? <span className="runIdBadge">Run {runId.slice(0, 8)}</span> : null}
        </div>
        <div className="toolbarActions">
          <button
            type="button"
            className="secondaryButton"
            onClick={() => void refreshHealth()}
            disabled={health.loading}
          >
            {health.loading ? "Checking…" : "Health"}
          </button>
          <button onClick={runPipeline} disabled={!canRun}>
            {running ? "Running…" : mode === "demo" ? "Run demo" : "Run pipeline"}
          </button>
        </div>
      </header>

      {connectionError ? <div className="appAlert error">{connectionError}</div> : null}
      {health.dockerAvailable === false && health.dockerRemediation ? (
        <div className="appAlert error">{health.dockerRemediation}</div>
      ) : null}

      <div className="appBody">
        <aside className="appSidebar">
          <div className="panel compactPanel">
            <h2>Pre-flight</h2>
            <ul className="healthList">
              <li className={health.backendOk ? "healthOk" : "healthBad"}>
                API {health.backendOk ? "ok" : "down"}
              </li>
              <li
                className={
                  health.dockerAvailable === true
                    ? "healthOk"
                    : health.dockerAvailable === false
                      ? "healthBad"
                      : "healthWarn"
                }
              >
                Docker{" "}
                {health.dockerAvailable === true
                  ? "ready"
                  : health.dockerAvailable === false
                    ? "down"
                    : "?"}
              </li>
              <li
                className={
                  health.dockerSmokeOk === true
                    ? "healthOk"
                    : health.dockerSmokeOk === false
                      ? "healthBad"
                      : "healthWarn"
                }
              >
                Smoke test{" "}
                {health.dockerSmokeOk === true
                  ? "pass"
                  : health.dockerSmokeOk === false
                    ? "fail"
                    : "—"}
              </li>
            </ul>
          </div>

          <div className="panel compactPanel">
            <h2>Pipeline</h2>
            <ol className="pipeline pipelineCompact">
              {displayPipeline.map((step) => {
                const status = stepStatus(
                  step.stage === "push" ? "rca" : step.stage,
                  scopedEvents,
                  terminal === "idle" ? "running" : terminal,
                );
                return (
                  <li key={step.stage} className={`pipelineStep ${status}`}>
                    <span className="dot" />
                    {step.label}
                  </li>
                );
              })}
              <li
                className={`pipelineStep ${
                  terminal === "done"
                    ? "complete"
                    : terminal === "failed"
                      ? "failed"
                      : "pending"
                }`}
              >
                <span className="dot" />
                {pushStepLabel}
              </li>
            </ol>
          </div>

          <div className="panel compactPanel">
            <h2>Agents</h2>
            <ul className="sidebarAgents">
              {agents.map((agent) => {
                const status = agentStatuses[agent.name] ?? "idle";
                return (
                  <li key={agent.name} className={`sidebarAgent sidebarAgent-${status}`}>
                    <span className="dot" />
                    <span>{agent.name}</span>
                  </li>
                );
              })}
            </ul>
          </div>
        </aside>

        <section className="appMain">
          <nav className="workspaceTabs" aria-label="Incident views">
            {WORKSPACE_TAB_ORDER.map((tab) => (
              <button
                key={tab}
                type="button"
                className={activeTab === tab ? "workspaceTab active" : "workspaceTab"}
                onClick={() => setActiveTab(tab)}
              >
                {TAB_LABELS[tab]}
                {tabBadges[tab] ? (
                  <span className="tabBadge">
                    {typeof tabBadges[tab] === "number" ? tabBadges[tab] : "•"}
                  </span>
                ) : null}
              </button>
            ))}
          </nav>

          <div className="tabPanel">
            {activeTab === "chat" && <AgentChat messages={chatMessages} isRunning={running} />}
            {activeTab === "report" && (
              <ReportTab
                rca={final.rca}
                terminal={terminal}
                repoName={final.repoName}
                branch={final.branch}
                prUrl={final.prUrl}
                prError={final.prError}
                failureMessage={final.failureMessage}
                errors={final.errors}
                fixExport={final.fixExport}
                runId={runId}
                apiBase={API_BASE}
              />
            )}
            {activeTab === "changes" && (
              <ChangesTab
                parsedFiles={parsedDiff}
                patchDiff={final.fix}
                testCode={final.testCode}
                testCommand={final.testCommand}
                filesFromExport={final.fixExport?.files_changed ?? []}
                runId={runId}
                apiBase={API_BASE}
                prUrl={final.prUrl}
                prError={final.prError}
                branch={final.branch}
                repoName={final.repoName}
                fixExport={final.fixExport}
              />
            )}
            {activeTab === "input" && (
              <InputTab
                mode={mode}
                setMode={setMode}
                repoUrl={repoUrl}
                setRepoUrl={setRepoUrl}
                errorText={errorText}
                setErrorText={setErrorText}
                impact={impact}
                setImpact={setImpact}
                githubToken={githubToken}
                setGithubToken={setGithubToken}
                demoAlert={demoAlert}
                submittedAlert={submittedAlert}
                runId={runId}
                agents={agents}
                showDebug={showDebug}
                setShowDebug={setShowDebug}
                scopedEvents={scopedEvents}
              />
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
