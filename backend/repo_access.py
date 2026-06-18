from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

from backend.git_output import resolve_github_token

_CODE_EXTENSIONS = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".java",
    ".rs", ".rb", ".md", ".json", ".yml", ".yaml", ".html", ".css",
)
_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".next", "dist", "build", ".ruff_cache", ".pytest_cache",
}


def repos_root() -> Path:
    return Path(
        os.getenv("REPOS_ROOT", Path.home() / ".band-repos")
    ).expanduser().resolve()


def _repo_path_from_url(repo_url: str) -> Path:
    """
    Derive a local clone destination from a GitHub URL.
    e.g. https://github.com/hamzaraza123/mock-buggy-project
         → ~/.band-repos/hamzaraza123_mock-buggy-project
    """
    clean = repo_url.rstrip("/").rstrip(".git")
    slug = clean.split("github.com/")[-1].replace("/", "_")
    return repos_root() / slug


def resolve_safe_repo_path(path: str | Path, *, repos_root_path: Path | None = None) -> Path:
    """Resolve repo_path inside REPOS_ROOT or the current project directory."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    else:
        candidate = candidate.resolve()

    allowed_roots = [repos_root_path or repos_root(), Path.cwd().resolve()]
    if not any(_is_relative_to(candidate, root) for root in allowed_roots):
        raise ValueError(f"repo_path must be under an allowed root: {candidate}")
    if not candidate.exists():
        raise FileNotFoundError(f"repo_path does not exist: {candidate}")
    if not candidate.is_dir():
        raise NotADirectoryError(f"repo_path is not a directory: {candidate}")
    return candidate


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def load_repo_files(
    repo_path: str | Path,
    *,
    max_files: int = 40,
    max_bytes_per_file: int = 12_000,
    max_total_bytes: int = 100_000,
) -> dict[str, str]:
    """Load a bounded slice of repository source for LLM context."""
    root = Path(repo_path).resolve()
    code_map: dict[str, str] = {}
    total_bytes = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for filename in filenames:
            if not filename.endswith(_CODE_EXTENSIONS):
                continue
            full_path = Path(dirpath) / filename
            try:
                rel = full_path.relative_to(root).as_posix()
            except ValueError:
                continue
            try:
                raw = full_path.read_bytes()
            except OSError:
                continue

            if len(raw) > max_bytes_per_file:
                chunk = raw[:max_bytes_per_file]
                text = chunk.decode("utf-8", errors="replace") + "\n... [truncated]"
                added_bytes = len(chunk)
            else:
                text = raw.decode("utf-8", errors="replace")
                added_bytes = len(raw)

            if total_bytes + added_bytes > max_total_bytes:
                continue

            code_map[rel] = text
            total_bytes += added_bytes

            if len(code_map) >= max_files:
                return code_map

    return code_map


def _clone_repo(repo_url: str, destination: Path, token: str | None, commit_sha: str | None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)

    clone_url = repo_url
    if token and clone_url.startswith("https://github.com/"):
        clone_url = clone_url.replace(
            "https://github.com/",
            f"https://x-access-token:{token}@github.com/",
            1,
        )

    if destination.exists() and (destination / ".git").is_dir():
        print(f"[repo] repo already cloned at {destination}, pulling latest...")
        _run_git(["git", "fetch", "--prune", "origin"], destination, token)
        base = os.getenv("GITHUB_PR_BASE_BRANCH", "main")
        _run_git(["git", "checkout", base], destination, token)
        _run_git(["git", "pull", "origin", base], destination, token)
        if commit_sha:
            _run_git(["git", "checkout", commit_sha], destination, token)
        return

    if destination.exists():
        raise FileExistsError(f"destination exists but is not a git repo: {destination}")

    print(f"[repo] cloning {repo_url} -> {destination} (token={'yes' if token else 'no'})")
    subprocess.run(
        ["git", "clone", "--depth", "1", clone_url, str(destination)],
        check=True,
        capture_output=True,
        text=True,
    )
    print(f"[repo] clone complete.")

    if commit_sha:
        _run_git(["git", "fetch", "--depth", "1", "origin", commit_sha], destination, token)
        _run_git(["git", "checkout", commit_sha], destination, token)


def _run_git(cmd: list[str], cwd: Path, token: str | None) -> None:
    env = os.environ.copy()
    if token:
        env["GIT_TERMINAL_PROMPT"] = "0"
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True, env=env)


def _resolve_token(alert: dict[str, Any]) -> str | None:
    """
    Resolve a GitHub token from (in order of preference):
    1. alert payload github_token field
    2. repo_full_name -> GitHub App token
    3. GITHUB_TOKEN env var
    """
    alert_token = alert.get("github_token")
    if isinstance(alert_token, str) and alert_token.strip():
        print("[repo] using github_token from alert payload")
        return alert_token.strip()

    repo_full_name = alert.get("repo_full_name")
    if repo_full_name:
        try:
            token = resolve_github_token(repo_full_name, None)
            if token:
                print("[repo] using github_token from GitHub App (repo_full_name)")
                return token
        except ValueError:
            pass

    env_token = os.getenv("GITHUB_TOKEN")
    if env_token:
        print("[repo] using GITHUB_TOKEN from environment")
        return env_token

    print("[repo] WARNING: no github_token found -- clone may fail for private repos")
    return None


async def ensure_repo_checkout(alert: dict[str, Any]) -> tuple[str, str | None]:
    """
    Ensure a local clone of the repo exists and return its path.

    Key fix: if alert has repo_url but no repo_path, we derive repo_path
    automatically from the URL so the clone can proceed.

    Returns (repo_path, error_message). error_message is None on success.
    """
    repo_url: str | None = alert.get("repo_url")
    repo_path: str | None = alert.get("repo_path")

    # FIX: derive repo_path from repo_url when not explicitly provided
    if not repo_path and repo_url:
        derived = _repo_path_from_url(repo_url)
        repo_path = str(derived)
        alert["repo_path"] = repo_path
        print(f"[repo] derived repo_path={repo_path} from repo_url={repo_url}")

    if not repo_path:
        return "", "repo_path is missing and repo_url was not provided -- cannot clone"

    # If it already exists locally, just return it
    try:
        resolved = resolve_safe_repo_path(repo_path)
        print(f"[repo] repo already exists at {resolved}")
        return str(resolved), None
    except FileNotFoundError:
        pass  # Does not exist yet -- proceed to clone
    except (ValueError, NotADirectoryError) as exc:
        return str(repo_path), str(exc)

    # Need to clone
    if not repo_url:
        return str(repo_path), (
            f"repository not found at {repo_path} and repo_url is missing -- cannot clone"
        )

    destination = Path(repo_path).expanduser()
    if not destination.is_absolute():
        destination = (Path.cwd() / destination).resolve()
    else:
        destination = destination.resolve()

    allowed_roots = [repos_root(), Path.cwd().resolve()]
    if not any(_is_relative_to(destination, root) for root in allowed_roots):
        return str(repo_path), (
            f"repo_path {destination} is not under an allowed root "
            f"({[str(r) for r in allowed_roots]}). "
            f"Set REPOS_ROOT env var to override."
        )

    token = _resolve_token(alert)

    try:
        await asyncio.to_thread(
            _clone_repo,
            repo_url,
            destination,
            token,
            alert.get("commit_sha"),
        )
        resolved = resolve_safe_repo_path(destination)
        print(f"[repo] successfully cloned to {resolved}")
        return str(resolved), None
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        print(f"[repo] git clone failed: {detail}")
        return str(repo_path), f"git clone failed: {detail}"
    except Exception as exc:
        print(f"[repo] unexpected error during clone: {exc}")
        return str(repo_path), str(exc)