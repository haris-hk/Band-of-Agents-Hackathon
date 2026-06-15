"use client";

import { FixExportPayload } from "./types";

async function copyText(text: string) {
  await navigator.clipboard.writeText(text);
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
}) {
  const steps = fixExport?.replication_steps ?? [];

  return (
    <div className="reportTab">
      {terminal === "failed" && (
        <div className="banner failed reportBanner">
          <strong>Pipeline failed</strong>
          {failureMessage ? <p>{failureMessage}</p> : null}
          {errors.length > 0 && (
            <ul>
              {errors.map((err, i) => (
                <li key={i}>{err}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {terminal === "done" && (
        <div className="banner success reportBanner">
          <strong>Incident resolved</strong>
          {repoName ? <p>Repository: {repoName}</p> : null}
          {branch ? <p>Branch: {branch}</p> : null}
          {prUrl ? (
            <p>
              <a href={prUrl} target="_blank" rel="noreferrer">
                Open pull request
              </a>
            </p>
          ) : null}
          {prError && !prUrl ? <p className="prWarning">{prError}</p> : null}
        </div>
      )}

      <section className="reportSection">
        <h3>Root cause analysis</h3>
        {rca ? (
          <pre className="reportMarkdown">{rca}</pre>
        ) : (
          <p className="muted">The RCA report will appear when the pipeline reaches the final stage.</p>
        )}
      </section>

      {(steps.length > 0 || fixExport?.apply_message) && (
        <section className="reportSection">
          <h3>Apply manually</h3>
          {fixExport?.applied_to_repo ? (
            <p className="hint successHint">
              Patch applied to local clone ({fixExport.apply_message ?? "ok"}).
            </p>
          ) : fixExport?.apply_message ? (
            <p className="prWarning">Could not auto-apply: {fixExport.apply_message}</p>
          ) : null}
          {steps.length > 0 && (
            <>
              <ol className="replicationSteps">
                {steps.map((step, index) => (
                  <li key={index}>
                    <code>{step}</code>
                  </li>
                ))}
              </ol>
              <button
                type="button"
                className="secondaryButton"
                onClick={() => void copyText(steps.join("\n"))}
              >
                Copy replication steps
              </button>
            </>
          )}
        </section>
      )}
    </div>
  );
}
