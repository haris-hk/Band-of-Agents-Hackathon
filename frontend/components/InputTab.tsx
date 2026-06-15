"use client";

import { AgentEvent, PipelineAgent } from "../lib/agentThread";

export function InputTab({
  mode,
  setMode,
  repoUrl,
  setRepoUrl,
  errorText,
  setErrorText,
  impact,
  setImpact,
  githubToken,
  setGithubToken,
  demoAlert,
  submittedAlert,
  runId,
  agents,
  showDebug,
  setShowDebug,
  scopedEvents,
}: {
  mode: "demo" | "github";
  setMode: (mode: "demo" | "github") => void;
  repoUrl: string;
  setRepoUrl: (v: string) => void;
  errorText: string;
  setErrorText: (v: string) => void;
  impact: string;
  setImpact: (v: string) => void;
  githubToken: string;
  setGithubToken: (v: string) => void;
  demoAlert: Record<string, unknown> | null;
  submittedAlert: Record<string, unknown> | null;
  runId: string | null;
  agents: PipelineAgent[];
  showDebug: boolean;
  setShowDebug: (v: boolean) => void;
  scopedEvents: AgentEvent[];
}) {
  return (
    <div className="inputTab">
      <section className="inputSection">
        <h3>Configure incident</h3>
        <div className="modeTabs">
          <button
            type="button"
            className={mode === "demo" ? "modeTab active" : "modeTab"}
            onClick={() => setMode("demo")}
          >
            Local demo
          </button>
          <button
            type="button"
            className={mode === "github" ? "modeTab active" : "modeTab"}
            onClick={() => setMode("github")}
          >
            GitHub repo
          </button>
        </div>

        {mode === "demo" ? (
          <>
            <p className="hint demoHint">
              Deterministic checkout bug in this repo — no GitHub token or LLM keys required.
            </p>
            {demoAlert ? (
              <dl className="inputFacts">
                <div>
                  <dt>Service</dt>
                  <dd>{String(demoAlert.service ?? "checkout")}</dd>
                </div>
                <div>
                  <dt>Error</dt>
                  <dd>{String(demoAlert.error ?? "—")}</dd>
                </div>
                <div>
                  <dt>Impact</dt>
                  <dd>{String(demoAlert.impact ?? "—")}</dd>
                </div>
                <div>
                  <dt>Repo path</dt>
                  <dd>
                    <code>{String(demoAlert.repo_path ?? ".")}</code>
                  </dd>
                </div>
              </dl>
            ) : (
              <p className="muted">Loading demo alert from backend…</p>
            )}
          </>
        ) : (
          <div className="formPanel">
            <label>
              GitHub repo URL
              <input
                type="url"
                value={repoUrl}
                onChange={(e) => setRepoUrl(e.target.value)}
                placeholder="https://github.com/org/repo"
              />
            </label>
            <label>
              Error / issue title
              <textarea rows={3} value={errorText} onChange={(e) => setErrorText(e.target.value)} />
            </label>
            <label>
              Impact
              <input type="text" value={impact} onChange={(e) => setImpact(e.target.value)} />
            </label>
            <label>
              GitHub token (optional)
              <input
                type="password"
                value={githubToken}
                onChange={(e) => setGithubToken(e.target.value)}
                placeholder="Uses server GITHUB_TOKEN if empty"
                autoComplete="off"
              />
            </label>
            <p className="hint">
              Requires server <code>GITHUB_TOKEN</code> and <code>LIVE_LLM_ENABLED=true</code>.
            </p>
          </div>
        )}
      </section>

      {(submittedAlert || runId) && (
        <section className="inputSection">
          <h3>Current run</h3>
          {runId ? (
            <p>
              Run ID: <code>{runId}</code>
            </p>
          ) : null}
          {submittedAlert && (
            <dl className="inputFacts">
              {Object.entries(submittedAlert)
                .filter(([key]) => key !== "github_token")
                .map(([key, value]) => (
                  <div key={key}>
                    <dt>{key.replace(/_/g, " ")}</dt>
                    <dd>
                      {typeof value === "object" ? (
                        <code>{JSON.stringify(value)}</code>
                      ) : (
                        String(value)
                      )}
                    </dd>
                  </div>
                ))}
            </dl>
          )}
        </section>
      )}

      <section className="inputSection">
        <h3>Agent roster</h3>
        <ul className="rosterList">
          {agents.map((agent) => (
            <li key={agent.name}>
              <strong>{agent.name}</strong>
              <span className="muted">{agent.mention}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className="inputSection inputSection-debug">
        <button
          type="button"
          className="threadToggle"
          onClick={() => setShowDebug(!showDebug)}
        >
          {showDebug ? "Hide developer event log" : "Show developer event log"}
        </button>
        {showDebug && (
          <div className="feed debugFeed">
            {scopedEvents.length === 0 ? (
              <p className="muted">Raw WebSocket JSON for debugging.</p>
            ) : (
              scopedEvents.map((event, index) => (
                <div key={index} className={`event event-${event.status}`}>
                  <div className="eventHeader">
                    <b>{event.status}</b>
                    <span>
                      {event.agent} / {event.stage}
                    </span>
                  </div>
                  {event.error ? <pre className="error">{event.error}</pre> : null}
                  <pre>{JSON.stringify(event.payload, null, 2)}</pre>
                </div>
              ))
            )}
          </div>
        )}
      </section>
    </div>
  );
}
