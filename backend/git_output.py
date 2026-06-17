"""
Applies a generated patch to a local repo clone, pushes the branch, and opens a GitHub PR.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from typing import Optional

from github import Auth, Github, GithubIntegration

from backend.schemas import IncidentState


def _run_cmd(cmd: list[str], cwd: str, extra_env: Optional[dict] = None) -> str:
    env = {**os.environ, **(extra_env or {})}
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        return result.stdout
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"Command `{' '.join(cmd)}` failed (exit {exc.returncode}):\n"
            f"stdout:\n{exc.stdout}\n\n"
            f"stderr:\n{exc.stderr}"
        ) from exc


def resolve_github_token(repo_full_name: str, override: str | None = None) -> str:
    """Resolve GitHub token: per-incident override, then App auth, then GITHUB_TOKEN."""
    if override and override.strip():
        return override.strip()
    return _resolve_token(repo_full_name)


def _resolve_token(repo_full_name: str) -> str:
    app_id = os.environ.get("GITHUB_APP_ID")
    private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY")

    if app_id and private_key:
        from pathlib import Path as _Path
        try:
            pem = _Path(private_key).read_text(encoding="utf-8") if os.path.isfile(private_key) else private_key
        except OSError as exc:
            raise ValueError(f"Could not read GITHUB_APP_PRIVATE_KEY file: {exc}") from exc
        auth = Auth.AppAuth(int(app_id), pem)
        gi = GithubIntegration(auth=auth)
        override_install_id = os.environ.get("GITHUB_APP_INSTALLATION_ID")
        if override_install_id:
            installation = gi.get_installation_by_id(int(override_install_id))
        else:
            owner, repo = repo_full_name.split("/", 1)
            installation = gi.get_repo_installation(owner, repo)
        return gi.get_access_token(installation.id).token

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("No GitHub credentials configured.")
    return token


def _push_fix_as_pr_sync(state: IncidentState, repo_path: str) -> str:
    if state.context is None:
        raise ValueError("Incident context is missing.")
    if state.rca is None:
        raise ValueError("RCA report has not been generated.")

    repo_full_name = state.repo_full_name or state.context.service
    if "/" not in repo_full_name:
        raise ValueError(f"repo_full_name must be org/repo, got: {repo_full_name}")

    repo_path = state.repo_path or repo_path
    if not repo_path:
        raise ValueError("repo_path is missing")

    override = state.raw_alert.payload.get("github_token")
    gh_token = resolve_github_token(
        repo_full_name,
        override if isinstance(override, str) else None,
    )
    g = Github(auth=Auth.Token(gh_token))
    gh_repo = g.get_repo(repo_full_name)

    branch_name = state.rca.git_branch
    commit_message = state.rca.commit_message
    patch_diff = state.rca.patch_unified_diff
    if not branch_name or not commit_message or not patch_diff:
        raise ValueError("RCA is missing git_branch, commit_message, or patch_unified_diff")

    _run_cmd(
        ["git", "config", "user.email", os.getenv("GIT_BOT_EMAIL", "bot@users.noreply.github.com")],
        cwd=repo_path,
    )
    _run_cmd(
        ["git", "config", "user.name", os.getenv("GIT_BOT_NAME", "Band Bot")],
        cwd=repo_path,
    )

    remote = f"https://x-access-token:{gh_token}@github.com/{repo_full_name}.git"
    _run_cmd(["git", "remote", "set-url", "origin", remote], cwd=repo_path)

    try:
        _run_cmd(["git", "checkout", "-b", branch_name], cwd=repo_path)
    except RuntimeError:
        _run_cmd(["git", "checkout", branch_name], cwd=repo_path)

    # ADDED newline="" to stop Windows from corrupting the patch file
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False, encoding="utf-8", newline=""
    ) as handle:
        handle.write(patch_diff)
        patch_file = handle.name
    # File must be closed before _run_cmd opens it (required on Windows).

    try:
        # Check if fix_export.py already applied the changes to the files
        already_applied = state.fix_export and state.fix_export.get("applied_to_repo")
        
        # Only apply the patch if it hasn't been applied yet!
        if not already_applied:
            _run_cmd(
                ["git", "apply", "-p1", "--ignore-space-change", "--whitespace=nowarn", patch_file],
                cwd=repo_path,
            )
    finally:
        try:
            os.unlink(patch_file)
        except OSError:
            pass

    _run_cmd(["git", "add", "-A"], cwd=repo_path)
    _run_cmd(["git", "commit", "-m", commit_message], cwd=repo_path)
    _run_cmd(["git", "push", "origin", branch_name], cwd=repo_path)

    base_branch = os.getenv("GITHUB_PR_BASE_BRANCH", "main")
    validation_block = ""
    if state.rca.validation_summary:
        validation_block = f"\n\n## Validation\n{state.rca.validation_summary}"

    pr_body = (
        f"## RCA\n\n{state.rca.final_markdown}"
        f"{validation_block}\n\n"
        "---\n"
        "*Auto-generated by Band-of-Agents*"
    )

    pr = gh_repo.create_pull(
        title=commit_message,
        body=pr_body,
        head=branch_name,
        base=base_branch,
    )
    return pr.html_url


async def push_fix_as_pr(state: IncidentState, repo_path: str) -> str:
    return await asyncio.to_thread(_push_fix_as_pr_sync, state, repo_path)
