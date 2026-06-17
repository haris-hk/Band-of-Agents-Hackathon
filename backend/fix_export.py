from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from backend.run_store import runs_dir

if TYPE_CHECKING:
    from backend.schemas import IncidentState


def _run_cmd(cmd: list[str], cwd: Path) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )
        return True, result.stdout.strip()
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        return False, detail
    except FileNotFoundError as exc:
        return False, str(exc)


def _strip_path(path: str, strip: int) -> str:
    parts = path.replace("\\", "/").split("/")
    if strip >= len(parts):
        return parts[-1] if parts else path
    return "/".join(parts[strip:])


def _apply_hunk(file_lines: list[str], hunk_lines: list[str], old_start: int) -> tuple[bool, str, list[str]]:
    result = file_lines[: max(old_start - 1, 0)]
    cursor = max(old_start - 1, 0)

    for row in hunk_lines:
        if not row or row.startswith("\\"):
            continue
        tag, content = row[0], row[1:]
        if tag == " ":
            if cursor >= len(file_lines) or file_lines[cursor] != content:
                return False, f"context mismatch near line {cursor + 1}", file_lines
            result.append(content)
            cursor += 1
        elif tag == "-":
            if cursor >= len(file_lines) or file_lines[cursor] != content:
                return False, f"delete mismatch near line {cursor + 1}", file_lines
            cursor += 1
        elif tag == "+":
            result.append(content)
        else:
            return False, f"unexpected diff line: {row}", file_lines

    result.extend(file_lines[cursor:])
    return True, "ok", result


def _parse_hunk_header(header: str) -> tuple[int, int]:
    old_part = header.split("@@")[1].strip().split(" ")[0]
    old_start = int(old_part.split(",")[0].lstrip("-"))
    return old_start, 0


def _apply_unified_diff_python(root: Path, patch_diff: str, strip: int = 1) -> tuple[bool, str]:
    """Minimal unified-diff applier for simple patches (Windows fallback)."""
    lines = patch_diff.replace("\r\n", "\n").splitlines()
    index = 0
    files_touched = 0

    while index < len(lines):
        if not lines[index].startswith("--- "):
            index += 1
            continue
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            return False, "malformed diff: missing +++ line"

        old_path = _strip_path(lines[index][4:].split("\t", 1)[0], strip)
        new_path = _strip_path(lines[index + 1][4:].split("\t", 1)[0], strip)
        target_path = root / (new_path or old_path)
        if not target_path.is_file():
            return False, f"target file not found: {target_path}"

        file_lines = target_path.read_text(encoding="utf-8").splitlines()
        hunks: list[tuple[int, list[str]]] = []
        index += 2

        while index < len(lines) and lines[index].startswith("@@"):
            old_start, _ = _parse_hunk_header(lines[index])
            index += 1
            hunk_lines: list[str] = []
            while index < len(lines) and not lines[index].startswith("@@") and not lines[index].startswith("--- "):
                hunk_lines.append(lines[index])
                index += 1
            hunks.append((old_start, hunk_lines))

        for old_start, hunk_lines in sorted(hunks, key=lambda item: item[0], reverse=True):
            ok, detail, file_lines = _apply_hunk(file_lines, hunk_lines, old_start)
            if not ok:
                return False, f"{target_path}: {detail}"

        trailing_newline = target_path.read_text(encoding="utf-8").endswith("\n")
        target_path.write_text(
            "\n".join(file_lines) + ("\n" if trailing_newline else ""),
            encoding="utf-8",
        )
        files_touched += 1

    if files_touched == 0:
        return False, "no applicable hunks found in patch"
    return True, "applied with built-in patch applier"


def apply_patch_to_repo(repo_path: str | Path, patch_diff: str, strip: int = 1) -> tuple[bool, str]:
    """Apply unified diff to a local clone (git apply, patch(1), then Python fallback)."""
    root = Path(repo_path).resolve()
    if not root.is_dir():
        return False, f"repo_path is not a directory: {root}"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False, encoding="utf-8") as handle:
        handle.write(patch_diff.replace("\r\n", "\n"))
        patch_file = handle.name

    try:
        ok, detail = _run_cmd(
            ["git", "apply", f"-p{strip}", "--whitespace=nowarn", patch_file],
            root,
        )
        if ok:
            return True, "applied with git apply"

        ok, detail2 = _run_cmd(
            ["patch", f"-p{strip}", "--batch", "--forward", "--fuzz=0", "-i", patch_file],
            root,
        )
        if ok:
            return True, "applied with patch"

        ok, detail3 = _apply_unified_diff_python(root, patch_diff, strip=strip)
        if ok:
            return True, detail3
        return False, detail3 or detail2 or detail
    finally:
        try:
            os.unlink(patch_file)
        except OSError:
            pass


def build_replication_steps(state: IncidentState) -> list[str]:
    steps: list[str] = []
    repo = state.repo_path or "."
    branch = state.rca.git_branch if state.rca else f"fix/incident-{str(state.run_id)[:8]}"
    commit = state.rca.commit_message if state.rca else "Apply validated incident fix"
    test_cmd = state.tests.run_command if state.tests else "pytest"

    steps.append(f"cd {repo}")
    steps.append(f"git checkout -b {branch}")
    steps.append("git apply -p1 fix.patch   # or: patch -p1 < fix.patch")
    if state.tests and state.tests.test_files:
        rel = state.tests.test_files[0]
        steps.append(f"# regression test: {rel}")
    steps.append(test_cmd)
    steps.append(f"git add -A && git commit -m \"{commit}\"")
    steps.append(f"git push -u origin {branch}")
    steps.append("# open a PR on GitHub (requires GITHUB_TOKEN on the server or your local credentials)")
    return steps


def export_validated_fix(state: IncidentState) -> dict:
    """
    Persist patch + test + replication guide; optionally apply patch on the host repo.
    """
    if state.fix is None:
        return {}

    run_id = str(state.run_id)
    export_root = runs_dir() / run_id / "fix"
    export_root.mkdir(parents=True, exist_ok=True)

    patch_path = export_root / "fix.patch"
    patch_path.write_text(state.fix.patch_unified_diff, encoding="utf-8")

    test_path: Path | None = None
    if state.tests:
        test_path = export_root / "regression_test.py"
        test_path.write_text(state.tests.test_code, encoding="utf-8")

    replication_steps = build_replication_steps(state)
    guide_lines = [
        "# Replicate this fix manually",
        "",
        "## Files changed",
        *[f"- {path}" for path in state.fix.files_changed],
        "",
        "## Steps",
        *[f"{index + 1}. {step}" for index, step in enumerate(replication_steps)],
        "",
        "## Patch",
        "```diff",
        state.fix.patch_unified_diff.rstrip(),
        "```",
    ]
    if state.tests:
        guide_lines.extend(
            [
                "",
                "## Regression test",
                "```python",
                state.tests.test_code.rstrip(),
                "```",
                "",
                f"Run: `{state.tests.run_command}`",
            ]
        )
    guide_path = export_root / "REPLICATE.md"
    guide_path.write_text("\n".join(guide_lines) + "\n", encoding="utf-8")

    applied = False
    apply_message: str | None = None
    if state.repo_path:
        applied, apply_message = apply_patch_to_repo(
            state.repo_path,
            state.fix.patch_unified_diff,
            strip=int(state.raw_alert.payload.get("patch_strip", 1)),
        )

    return {
        "patch_path": str(patch_path),
        "test_path": str(test_path) if test_path else None,
        "guide_path": str(guide_path),
        "applied_to_repo": applied,
        "apply_message": apply_message,
        "replication_steps": replication_steps,
        "files_changed": list(state.fix.files_changed),
        "test_command": state.tests.run_command if state.tests else None,
        "branch_name": state.rca.git_branch if state.rca else None,
        "commit_message": state.rca.commit_message if state.rca else None,
        "patch_unified_diff": state.fix.patch_unified_diff,
        "test_code": state.tests.test_code if state.tests else None,
    }
