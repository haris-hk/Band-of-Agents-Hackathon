"""Python mirror of frontend/lib/parseDiff.ts for regression tests only."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DiffRow:
    kind: str
    old_num: int | None
    new_num: int | None
    text: str


@dataclass
class ParsedFileDiff:
    old_path: str
    new_path: str
    display_path: str
    rows: list[DiffRow]


def _strip_diff_prefix(path: str) -> str:
    return path.removeprefix("a/").removeprefix("b/")


def parse_unified_diff(diff: str) -> list[ParsedFileDiff]:
    if not diff.strip():
        return []

    lines = diff.replace("\r\n", "\n").split("\n")
    files: list[ParsedFileDiff] = []
    index = 0

    while index < len(lines):
        if not lines[index].startswith("--- "):
            index += 1
            continue
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            index += 1
            continue

        old_path = _strip_diff_prefix(lines[index][4:].split("\t", 1)[0])
        new_path = _strip_diff_prefix(lines[index + 1][4:].split("\t", 1)[0])
        display_path = new_path or old_path
        rows: list[DiffRow] = []
        index += 2

        while index < len(lines) and not lines[index].startswith("--- "):
            line = lines[index]
            if line.startswith("@@"):
                header = line
                old_start = int(header.split("+")[0].split(",")[0].replace("@@ -", "").strip())
                new_start = int(header.split("+")[1].split(",")[0].strip())
                rows.append(DiffRow(kind="hunk", old_num=None, new_num=None, text=line))
                index += 1
                old_line = old_start
                new_line = new_start

                while (
                    index < len(lines)
                    and not lines[index].startswith("@@")
                    and not lines[index].startswith("--- ")
                ):
                    row = lines[index]
                    if not row:
                        index += 1
                        continue
                    tag, content = row[0], row[1:]
                    if tag == " ":
                        rows.append(DiffRow("context", old_line, new_line, content))
                        old_line += 1
                        new_line += 1
                    elif tag == "-":
                        rows.append(DiffRow("remove", old_line, None, content))
                        old_line += 1
                    elif tag == "+":
                        rows.append(DiffRow("add", None, new_line, content))
                        new_line += 1
                    index += 1
                continue
            index += 1

        files.append(ParsedFileDiff(old_path, new_path, display_path, rows))

    return files
