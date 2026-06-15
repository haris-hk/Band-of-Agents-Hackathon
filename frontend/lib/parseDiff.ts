export type DiffRow = {
  kind: "hunk" | "add" | "remove" | "context";
  oldNum: number | null;
  newNum: number | null;
  text: string;
};

export type ParsedFileDiff = {
  oldPath: string;
  newPath: string;
  displayPath: string;
  rows: DiffRow[];
};

function stripDiffPrefix(path: string): string {
  return path.replace(/^a\//, "").replace(/^b\//, "");
}

function parseHunkHeader(line: string): { oldStart: number; newStart: number } | null {
  const match = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
  if (!match) return null;
  return { oldStart: Number(match[1]), newStart: Number(match[2]) };
}

export function parseUnifiedDiff(diff: string): ParsedFileDiff[] {
  if (!diff.trim()) return [];

  const lines = diff.replace(/\r\n/g, "\n").split("\n");
  const files: ParsedFileDiff[] = [];
  let index = 0;

  while (index < lines.length) {
    if (!lines[index].startsWith("--- ")) {
      index += 1;
      continue;
    }
    if (index + 1 >= lines.length || !lines[index + 1].startsWith("+++ ")) {
      index += 1;
      continue;
    }

    const oldPath = stripDiffPrefix(lines[index].slice(4).split("\t")[0]);
    const newPath = stripDiffPrefix(lines[index + 1].slice(4).split("\t")[0]);
    const displayPath = newPath || oldPath;
    const rows: DiffRow[] = [];
    index += 2;

    while (index < lines.length && !lines[index].startsWith("--- ")) {
      const line = lines[index];
      if (line.startsWith("@@")) {
        const header = parseHunkHeader(line);
        rows.push({
          kind: "hunk",
          oldNum: null,
          newNum: null,
          text: line,
        });
        index += 1;
        let oldLine = header?.oldStart ?? 1;
        let newLine = header?.newStart ?? 1;

        while (
          index < lines.length &&
          !lines[index].startsWith("@@") &&
          !lines[index].startsWith("--- ")
        ) {
          const row = lines[index];
          if (!row) {
            index += 1;
            continue;
          }
          const tag = row[0];
          const content = row.slice(1);

          if (tag === " ") {
            rows.push({
              kind: "context",
              oldNum: oldLine,
              newNum: newLine,
              text: content,
            });
            oldLine += 1;
            newLine += 1;
          } else if (tag === "-") {
            rows.push({
              kind: "remove",
              oldNum: oldLine,
              newNum: null,
              text: content,
            });
            oldLine += 1;
          } else if (tag === "+") {
            rows.push({
              kind: "add",
              oldNum: null,
              newNum: newLine,
              text: content,
            });
            newLine += 1;
          } else if (tag === "\\") {
            // no-op for "\\ No newline at end of file"
          }
          index += 1;
        }
        continue;
      }
      index += 1;
    }

    files.push({ oldPath, newPath, displayPath, rows });
  }

  return files;
}

export function collectChangedFiles(parsed: ParsedFileDiff[]): string[] {
  return parsed.map((file) => file.displayPath);
}
