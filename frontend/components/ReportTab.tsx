"use client";

import { FixExportPayload } from "./types";

async function copyText(text: string) {
  await navigator.clipboard.writeText(text);
}

function MarkdownReport({ text }: { text: string }) {
  const lines = text.split("\n");
  return (
    <div className="reportMarkdown">
      {lines.map((line, i) => {
        if (line.startsWith("# ")) return <h2 key={i} className="reportH1">{line.slice(2)}</h2>;
        if (line.startsWith("## ")) return <h3 key={i} className="reportH2">{line.slice(3)}</h3>;
        if (line.startsWith("### ")) return <h4 key={i} className="reportH3">{line.slice(4)}</h4>;
        if (line.startsWith("- ") || line.startsWith("* ")) {
          return <li key={i} className="reportLi">{line.slice(2)}</li>;
        }
        if (line.trim() === "") return <div key={i} className="reportBreak" />;
        const parts = line.split(/(`[^`]+`|\*\*[^*]+\*\*)/g);
        return (
          <p key={i} className="reportP">
            {parts.map((part, pi) => {
              if (part.startsWith("**") && part.endsWith("**"))
                return <strong key={pi}>{part.slice(2, -2)}</strong>;
              if (part.startsWith("`") && part.endsWith("`"))
                return <code key={pi} className="inlineCode">{part.slice(1, -1)}</code>;
              return <span key={pi}>{part}</span>;
            })}
          </p>
        );
      })}
    </div>
  );
}

export function ReportTab({
  rca,
  terminal,
  repoName,
  branch,
  prUrl,
  prError,
  failureMessage,
  errors,
  fixExport,
  runId,
  apiBase,
}: {
  rca: string;
  terminal: "running" | "done" | "failed" | "idle";
  repoName: string;
  branch: string;
  prUrl: string;
  prError: string;
  failureMessage: string;
  errors: string[];
  fixExport: FixExportPayload | null;
  runId?: string | null;
  apiBase?: string;
}) {
  const steps = fixExport?.replication_steps ?? [];

  return (
    <div className="reportTab">
      {/* Status banner */}
      {terminal === "failed" && (
        <div className="reportBanner reportBanner-failed">
          <div className="bannerIcon">✗</div>
          <div className="bannerBody">
            <strong>Pipeline encountered a failure</strong>
            {failureMessage ? <p>{failureMessage}</p> : null}
            {errors.length > 0 && (
              <ul className="bannerErrors">
                {errors.map((err, i) => <li key={i}>{err}</li>)}
              </ul>
            )}
          </div>
        </div>
      )}

      {terminal === "done" && (
        <div className="reportBanner reportBanner-success">
          <div className="bannerIcon">✓</div>
          <div className="bannerBody">
            <strong>Incident resolved</strong>
            {repoName && <p>Repository: <code>{repoName}</code></p>}
            {branch && <p>Branch: <code>{branch}</code></p>}
            {prUrl ? (
              <a href={prUrl} target="_blank" rel="noreferrer" className="prLinkBanner">
                Open pull request →
              </a>
            ) : prError && !prUrl ? (
              <p className="prWarning">{prError}</p>
            ) : null}
          </div>
        </div>
      )}

      {terminal === "running" && (
        <div className="reportBanner reportBanner-running">
          <div className="bannerDots">
            <span /><span /><span />
          </div>
          <div className="bannerBody">
            <strong>Pipeline running</strong>
            <p>Report will populate as each stage completes.</p>
          </div>
        </div>
      )}

      {/* RCA Report */}
      <section className="reportSection">
        <div className="reportSectionHeader">
          <h3>Root Cause Analysis</h3>
          <div className="reportSectionActions">
            {rca && (
              <button type="button" className="secondaryButton" onClick={() => void copyText(rca)}>
                Copy
              </button>
            )}
            {runId && apiBase && (terminal === "done" || terminal === "failed") && (
              <a
                className="primaryButton"
                href={`${apiBase}/runs/${runId}/report.html`}
                download={`incident-${runId.slice(0, 8)}.html`}
              >
                ⬇ Download Report
              </a>
            )}
          </div>
        </div>
        {rca ? (
          <MarkdownReport text={rca} />
        ) : (
          <p className="muted">
            {terminal === "idle"
              ? "Run a pipeline to generate the RCA report."
              : "RCA report will appear when the pipeline reaches the final stage."}
          </p>
        )}
      </section>

      {/* Validation summary */}
      {fixExport?.applied_to_repo !== undefined && (
        <section className="reportSection">
          <h3>Patch Applied</h3>
          {fixExport.applied_to_repo ? (
            <div className="applySuccess">
              <span>✅</span>
              <p>Patch successfully applied to local clone. {fixExport.apply_message}</p>
            </div>
          ) : (
            <div className="applyWarn">
              <span>⚠️</span>
              <p>Could not auto-apply: {fixExport.apply_message ?? "unknown error"}</p>
            </div>
          )}
        </section>
      )}

      {/* Replication steps */}
      {steps.length > 0 && (
        <section className="reportSection">
          <div className="reportSectionHeader">
            <h3>Apply Manually</h3>
            <button
              type="button"
              className="secondaryButton"
              onClick={() => void copyText(steps.join("\n"))}
            >
              Copy steps
            </button>
          </div>
          <ol className="replicationSteps">
            {steps.map((step, i) => (
              <li key={i}>
                <code>{step}</code>
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}
