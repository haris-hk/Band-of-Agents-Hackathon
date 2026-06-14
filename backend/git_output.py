# """
# backend/git_output.py
# ─────────────────────
# Applies a generated patch to a local repo clone, pushes the branch, and
# opens a GitHub PR.  Supports both PAT auth and GitHub App auth.
# """

# from __future__ import annotations

# import json
# import os
# import subprocess
# import tempfile
# from typing import Optional

# from github import Auth, Github, GithubIntegration

# from backend.schemas import IncidentState


# # ─────────────────────────────────────────────────────────────────────────────
# # Internal helpers
# # ─────────────────────────────────────────────────────────────────────────────

# def _run_cmd(cmd: list[str], cwd: str, extra_env: Optional[dict] = None) -> str:
#     """
#     Run a shell command synchronously.
#     Raises RuntimeError with stdout/stderr context on failure.
#     """
#     env = {**os.environ, **(extra_env or {})}
#     try:
#         result = subprocess.run(
#             cmd,
#             cwd=cwd,
#             check=True,
#             capture_output=True,
#             text=True,
#             env=env,
#         )
#         return result.stdout
#     except subprocess.CalledProcessError as exc:
#         raise RuntimeError(
#             f"Command `{' '.join(cmd)}` failed (exit {exc.returncode}):\n"
#             f"  stdout: {exc.stdout.strip()}\n"
#             f"  stderr: {exc.stderr.strip()}"
#         ) from exc


# def _resolve_token(repo_full_name: str) -> str:
#     """
#     Return a short-lived access token.

#     Priority:
#       1. GitHub App  (GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY)
#       2. PAT         (GITHUB_TOKEN)
#     """
#     app_id = os.environ.get("GITHUB_APP_ID")
#     private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY")

#     if app_id and private_key:
#         # ── GitHub App ──────────────────────────────────────────────────
#         # The private key may be a file path or the raw PEM string
#         pem = (
#             open(private_key).read()
#             if os.path.isfile(private_key)
#             else private_key
#         )
#         auth = Auth.AppAuth(int(app_id), pem)
#         gi = GithubIntegration(auth=auth)

#         override_install_id = os.environ.get("GITHUB_APP_INSTALLATION_ID")
#         if override_install_id:
#             installation = gi.get_installation_by_id(int(override_install_id))
#         else:
#             owner, repo_name = repo_full_name.split("/", 1)
#             installation = gi.get_repo_installation(owner, repo_name)

#         access_token = gi.get_access_token(installation.id)
#         return access_token.token

#     # ── Personal Access Token (fallback) ────────────────────────────────
#     token = os.environ.get("GITHUB_TOKEN")
#     if not token:
#         raise ValueError(
#             "No GitHub credentials configured.\n"
#             "  Option A (recommended): set GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY\n"
#             "  Option B (quick start): set GITHUB_TOKEN to a classic PAT with repo scope"
#         )
#     return token


# # ─────────────────────────────────────────────────────────────────────────────
# # Public API
# # ─────────────────────────────────────────────────────────────────────────────

# async def push_fix_as_pr(state: IncidentState, repo_path: str) -> str:
#     """
#     Apply `state.rca.patch_unified_diff` to the local clone at `repo_path`,
#     push a new branch, and open a GitHub PR.

#     Returns the HTML URL of the created PR.
#     """

#     # ── 1. Credentials ─────────────────────────────────────────────────────
#     repo_full_name: str = state.incident.service   # "org/repo"
#     gh_token = _resolve_token(repo_full_name)

#     g = Github(auth=Auth.Token(gh_token))
#     gh_repo = g.get_repo(repo_full_name)

#     # ── 2. Fields from RCA ─────────────────────────────────────────────────
#     branch_name: str = state.rca.git_branch
#     commit_message: str = state.rca.commit_message
#     patch_diff: str = state.rca.patch_unified_diff

#     # ── 3. Configure git identity ──────────────────────────────────────────
#     bot_email = os.environ.get(
#         "GIT_BOT_EMAIL", "band-of-agents[bot]@users.noreply.github.com"
#     )
#     bot_name = os.environ.get("GIT_BOT_NAME", "Band-of-Agents Bot")

#     _run_cmd(["git", "config", "user.email", bot_email], cwd=repo_path)
#     _run_cmd(["git", "config", "user.name", bot_name], cwd=repo_path)

#     # Embed the token in the remote URL so `git push` works without a keychain.
#     # For GitHub App tokens this is: https://x-access-token:<token>@github.com/...
#     authenticated_remote = (
#         f"https://x-access-token:{gh_token}@github.com/{repo_full_name}.git"
#     )
#     _run_cmd(
#         ["git", "remote", "set-url", "origin", authenticated_remote],
#         cwd=repo_path,
#     )

#     # ── 4. Create / switch to branch ───────────────────────────────────────
#     try:
#         _run_cmd(["git", "checkout", "-b", branch_name], cwd=repo_path)
#     except RuntimeError:
#         # Branch already exists (e.g. pipeline retry) — just switch to it
#         _run_cmd(["git", "checkout", branch_name], cwd=repo_path)

#     # ── 5. Apply the unified diff ──────────────────────────────────────────
#     with tempfile.NamedTemporaryFile(
#         mode="w", suffix=".patch", delete=False, dir="/tmp"
#     ) as fh:
#         fh.write(patch_diff)
#         patch_file = fh.name

#     try:
#         _run_cmd(["patch", "-p1", "--input", patch_file], cwd=repo_path)
#     finally:
#         try:
#             os.unlink(patch_file)
#         except OSError:
#             pass

#     # ── 6. Stage → commit → push ───────────────────────────────────────────
#     _run_cmd(["git", "add", "-A"], cwd=repo_path)
#     _run_cmd(["git", "commit", "-m", commit_message], cwd=repo_path)
#     _run_cmd(["git", "push", "origin", branch_name], cwd=repo_path)

#     # ── 7. Open the PR ─────────────────────────────────────────────────────
#     rca_summary = getattr(state.rca, "summary", "No RCA summary provided.")

#     validation_block = ""
#     if state.validation:
#         val_summary = getattr(state.validation, "summary", "No validation summary.")
#         validation_block = f"\n\n## ✅ Validation Results\n{val_summary}"

#     pr_body = (
#         f"## 🔍 Root Cause Analysis\n{rca_summary}"
#         f"{validation_block}\n\n"
#         f"---\n"
#         f"*Auto-generated by **Band-of-Agents** incident response pipeline.*"
#     )

#     pr = gh_repo.create_pull(
#         title=commit_message,
#         body=pr_body,
#         head=branch_name,
#         base="main",
#     )

#     return pr.html_url
"""
backend/git_output.py
─────────────────────
Applies a generated patch to a local repo clone, pushes the branch, and
opens a GitHub PR. Supports both PAT auth and GitHub App auth.
"""

from __future__ import annotations

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


def _resolve_token(repo_full_name: str) -> str:
    app_id = os.environ.get("GITHUB_APP_ID")
    private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY")

    if app_id and private_key:
        pem = (
            open(private_key).read()
            if os.path.isfile(private_key)
            else private_key
        )

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


async def push_fix_as_pr(state: IncidentState, repo_path: str) -> str:
    if state.context is None:
        raise ValueError("Incident context is missing.")

    if state.rca is None:
        raise ValueError("RCA report has not been generated.")

    # ── repo config (safe fallback) ─────────────────────────────
    repo_full_name = state.repo_full_name or state.context.service
    repo_path = state.repo_path or repo_path

    if not repo_path:
        raise ValueError("repo_path is missing")

    gh_token = _resolve_token(repo_full_name)

    g = Github(auth=Auth.Token(gh_token))
    gh_repo = g.get_repo(repo_full_name)

    branch_name = state.rca.git_branch
    commit_message = state.rca.commit_message
    patch_diff = state.rca.patch_unified_diff

    if not branch_name:
        raise ValueError("Missing git_branch")

    if not commit_message:
        raise ValueError("Missing commit_message")

    if not patch_diff:
        raise ValueError("Missing patch_unified_diff")

    # ── git identity ─────────────────────────────
    _run_cmd(
        ["git", "config", "user.email",
         os.environ.get("GIT_BOT_EMAIL", "bot@users.noreply.github.com")],
        cwd=repo_path,
    )

    _run_cmd(
        ["git", "config", "user.name",
         os.environ.get("GIT_BOT_NAME", "Band Bot")],
        cwd=repo_path,
    )

    # ── auth remote ─────────────────────────────
    remote = f"https://x-access-token:{gh_token}@github.com/{repo_full_name}.git"

    _run_cmd(["git", "remote", "set-url", "origin", remote], cwd=repo_path)

    # ── branch ─────────────────────────────
    try:
        _run_cmd(["git", "checkout", "-b", branch_name], cwd=repo_path)
    except RuntimeError:
        _run_cmd(["git", "checkout", branch_name], cwd=repo_path)

    # ── apply patch ─────────────────────────────
    with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as f:
        f.write(patch_diff)
        patch_file = f.name

    try:
        _run_cmd(["git", "apply", patch_file], cwd=repo_path)
    finally:
        os.unlink(patch_file)

    # ── commit + push ─────────────────────────────
    _run_cmd(["git", "add", "-A"], cwd=repo_path)
    _run_cmd(["git", "commit", "-m", commit_message], cwd=repo_path)
    _run_cmd(["git", "push", "origin", branch_name], cwd=repo_path)

    # ── PR body ─────────────────────────────
    rca_summary = state.rca.final_markdown

    validation_block = ""
    if state.rca.validation_summary:
        validation_block = f"\n\n## Validation\n{state.rca.validation_summary}"

    pr_body = (
        f"## RCA\n\n{rca_summary}"
        f"{validation_block}\n\n"
        "---\n"
        "*Auto-generated by Band system*"
    )

    pr = gh_repo.create_pull(
        title=commit_message,
        body=pr_body,
        head=branch_name,
        base="main",
    )

    return pr.html_url