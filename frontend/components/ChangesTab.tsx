"use client";

import { useState } from "react";
import { ParsedFileDiff } from "../lib/parseDiff";
import { FixExportPayload } from "./types";

async function copyText(text: string) {
  await navigator.clipboard.writeText(text);
}

function FileDiffBlock({ file }: { file: ParsedFileDiff }) {
  const [open, setOpen] = useState(true);
  const addCount = file.rows.filter((r) => r.kind === "add").length;
  const removeCount = file.rows.filter((r) => r.kind === "remove").length;

  return (
    <section className="changesSection fileDiffBlock">
      <button
        type="button"
        className="fileDiffHeader"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="fileDiffToggle">{open ? "▾" : "▸"}</span>
        <code className="fileDiffPath">{file.displayPath}</code>
        <span className="diffStat addStat">+{addCount}</span>
        <span className="diffStat removeStat">−{removeCount}</span>
      </button>

      {open && (
        <div className="diffTableWrap">
          <table className="diffTable">
            <thead>
              <tr>
                <th className="diffLineCol" title="Old line number">Old</th>
                <th className="diffLineCol" title="New line number">New</th>
                <th>Code</th>
              </tr>
            </thead>
            <tbody>
              {file.rows.map((row, index) => {
                if (row.kind === "hunk") {
                  return (
                    <tr key={index} className="diffRow-hunk">
                      <td colSpan={3}>
                        <code>{row.text}</code>
                      </td>
                    </tr>
                  );
                }
                return (
                  <tr key={index} className={`diffRow diffRow-${row.kind}`}>
                    <td className="diffLineNum">{row.oldNum ?? ""}</td>
                    <td className="diffLineNum">{row.newNum ?? ""}</td>
                    <td className="diffLineText">
                      <code>{row.text || " "}</code>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function PushPrPanel({
  fixExport,
  prUrl,
  prError,
  branch,
  repoName,
  apiBase,
  runId,
}: {
  fixExport: FixExportPayload | null;
  prUrl: string;
  prError: string;
  branch: string;
  repoName: string;
  apiBase: string;
  runId: string | null;
}) {
  const downloadUrl = runId ? `${apiBase}/runs/${runId}/fix.patch` : null;

  if (prUrl) {
    return (
      <div className="pushPanel pushPanel-success">
        <span className="pushIcon">✅</span>
        <div>
          <strong>Pull request is open</strong>
          <p>
            Branch <code>{branch}</code> on <strong>{repoName}</strong>
          </p>
          <a href={prUrl} target="_blank" rel="noreferrer" className="prLink">
            View pull request →
          </a>
        </div>
      </div>
    );
  }

  if (branch) {
    return (
      <div className="pushPanel">
        <span className="pushIcon">🌿</span>
        <div>
          <strong>Branch ready: <code>{branch}</code></strong>
          {repoName && <p>Repository: <strong>{repoName}</strong></p>}
          {prError && (
            <p className="prWarning">
              Auto-PR skipped: {prError}
            </p>
          )}
          <div className="pushActions">
            {downloadUrl && (
              <a className="primaryButton" href={downloadUrl} download="fix.patch">
                ⬇ Download fix.patch
              </a>
            )}
            <p className="hint">
              Apply: <code>git apply -p1 fix.patch</code> → push branch → open PR manually.
            </p>
          </div>
        </div>
      </div>
    );
  }

  if (fixExport?.applied_to_repo) {
    return (
      <div className="pushPanel pushPanel-applied">
        <span className="pushIcon">✅</span>
        <div>
          <strong>Patch auto-applied to local clone</strong>
          <p className="hint">{fixExport.apply_message ?? "Applied successfully."}</p>
          {downloadUrl && (
            <a className="secondaryButton" href={downloadUrl} download="fix.patch">
              ⬇ Download fix.patch
            </a>
          )}
        </div>
      </div>
    );
  }

  return null;
}

export function ChangesTab({
  parsedFiles,
  patchDiff,
  testCode,
  testCommand,
  filesFromExport,
  runId,
  apiBase,
  prUrl,
  prError,
  branch,
  repoName,
  fixExport,
}: {
  parsedFiles: ParsedFileDiff[];
  patchDiff: string;
  testCode: string;
  testCommand: string;
  filesFromExport: string[];
  runId: string | null;
  apiBase: string;
  prUrl: string;
  prError: string;
  branch: string;
  repoName: string;
  fixExport: FixExportPayload | null;
}) {
  const filePaths =
    filesFromExport.length > 0
      ? filesFromExport
      : parsedFiles.map((file) => file.displayPath);

  const downloadUrl = runId ? `${apiBase}/runs/${runId}/fix.patch` : null;

  const hasPushInfo = !!(prUrl || branch);

  if (!patchDiff && !testCode) {
    return (
      <div className="tabEmpty">
        <div className="tabEmptyIcon">📄</div>
        <p>No code changes yet.</p>
        <p className="muted">
          Validated patches and regression tests appear here after the fix and validate stages complete.
        </p>
      </div>
    );
  }

  return (
    <div className="changesTab">
      {/* Push / PR status panel */}
      {hasPushInfo && (
        <PushPrPanel
          fixExport={fixExport}
          prUrl={prUrl}
          prError={prError}
          branch={branch}
          repoName={repoName}
          apiBase={apiBase}
          runId={runId}
        />
      )}

      {/* Summary of changed files */}
      {filePaths.length > 0 && (
        <section className="changesSection">
          <h3>Files changed ({filePaths.length})</h3>
          <ul className="changesFileList">
            {filePaths.map((path) => (
              <li key={path}>
                <code>📄 {path}</code>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Per-file interactive diff viewer */}
      {parsedFiles.length > 0 ? (
        <>
          <div className="diffFilesHeader">
            <h3>Code changes — {parsedFiles.length} file{parsedFiles.length !== 1 ? "s" : ""}</h3>
            <div className="diffActions">
              <button
                type="button"
                className="secondaryButton"
                onClick={() => void copyText(patchDiff)}
              >
                Copy patch
              </button>
              {downloadUrl && (
                <a className="secondaryButton downloadLink" href={downloadUrl} download="fix.patch">
                  ⬇ Download .patch
                </a>
              )}
            </div>
          </div>
          {parsedFiles.map((file) => (
            <FileDiffBlock key={file.displayPath} file={file} />
          ))}
        </>
      ) : patchDiff ? (
        <section className="changesSection">
          <div className="changesSectionHeader">
            <h3>Patch (raw)</h3>
            <div className="diffActions">
              <button
                type="button"
                className="secondaryButton"
                onClick={() => void copyText(patchDiff)}
              >
                Copy patch
              </button>
              {downloadUrl && (
                <a className="secondaryButton downloadLink" href={downloadUrl} download="fix.patch">
                  ⬇ Download .patch
                </a>
              )}
            </div>
          </div>
          <pre className="codeText">{patchDiff}</pre>
        </section>
      ) : null}

      {/* Regression test */}
      {testCode ? (
        <section className="changesSection">
          <div className="changesSectionHeader">
            <h3>Regression test</h3>
            <div className="diffActions">
              {testCommand ? (
                <span className="testCommand">
                  <code>{testCommand}</code>
                </span>
              ) : null}
              <button
                type="button"
                className="secondaryButton"
                onClick={() => void copyText(testCode)}
              >
                Copy test
              </button>
            </div>
          </div>
          <pre className="codeText">{testCode}</pre>
        </section>
      ) : null}
    </div>
  );
}
