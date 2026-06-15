"use client";

import { ParsedFileDiff } from "../lib/parseDiff";

async function copyText(text: string) {
  await navigator.clipboard.writeText(text);
}

export function ChangesTab({
  parsedFiles,
  patchDiff,
  testCode,
  testCommand,
  filesFromExport,
  runId,
  apiBase,
}: {
  parsedFiles: ParsedFileDiff[];
  patchDiff: string;
  testCode: string;
  testCommand: string;
  filesFromExport: string[];
  runId: string | null;
  apiBase: string;
}) {
  const filePaths =
    filesFromExport.length > 0
      ? filesFromExport
      : parsedFiles.map((file) => file.displayPath);

  const downloadUrl = runId ? `${apiBase}/runs/${runId}/fix.patch` : null;

  if (!patchDiff && !testCode) {
    return (
      <div className="tabEmpty">
        <p>No code changes yet.</p>
        <p className="muted">Validated patches and regression tests show up after the fix stage.</p>
      </div>
    );
  }

  return (
    <div className="changesTab">
      {filePaths.length > 0 && (
        <section className="changesSection">
          <h3>Files touched</h3>
          <ul className="changesFileList">
            {filePaths.map((path) => (
              <li key={path}>
                <code>{path}</code>
              </li>
            ))}
          </ul>
        </section>
      )}

      {parsedFiles.length > 0 ? (
        parsedFiles.map((file) => (
          <section key={file.displayPath} className="changesSection">
            <h3>{file.displayPath}</h3>
            <div className="diffTableWrap">
              <table className="diffTable">
                <thead>
                  <tr>
                    <th className="diffLineCol">Old</th>
                    <th className="diffLineCol">New</th>
                    <th>Line</th>
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
          </section>
        ))
      ) : patchDiff ? (
        <section className="changesSection">
          <h3>Patch</h3>
          <pre className="codeText">{patchDiff}</pre>
        </section>
      ) : null}

      {testCode ? (
        <section className="changesSection">
          <div className="changesSectionHeader">
            <h3>Regression test</h3>
            {testCommand ? <span className="muted">{testCommand}</span> : null}
          </div>
          <pre className="codeText">{testCode}</pre>
        </section>
      ) : null}

      <div className="fixActions">
        {patchDiff ? (
          <>
            <button type="button" className="secondaryButton" onClick={() => void copyText(patchDiff)}>
              Copy patch
            </button>
            {downloadUrl ? (
              <a className="downloadLink" href={downloadUrl} download="fix.patch">
                Download fix.patch
              </a>
            ) : null}
          </>
        ) : null}
        {testCode ? (
          <button type="button" className="secondaryButton" onClick={() => void copyText(testCode)}>
            Copy test
          </button>
        ) : null}
      </div>
    </div>
  );
}
