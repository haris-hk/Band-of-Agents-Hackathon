from __future__ import annotations

from typing import Tuple


def humanize_docker_error(error: str | None) -> str:
    if not error:
        return "Docker is unavailable."
    lower = error.lower()
    if "read-only file system" in lower or (
        "read-only" in lower and "overlayfs" in lower
    ):
        return (
            "Docker Desktop storage is read-only (containerd overlayfs). "
            "Fix: quit Docker Desktop completely, start it again, then run "
            "`docker system prune -f`. If the error persists, use "
            "Docker Desktop → Troubleshoot → Clean / Purge data (last resort)."
        )
    if "unable to start" in lower or "503 server error" in lower:
        return (
            "Docker Desktop is not running or failed to start. "
            "Quit Docker Desktop from the system tray, reopen it, and wait until "
            "it shows Running. If it keeps failing, restart Windows or reset "
            "Docker Desktop from Troubleshoot."
        )
    if "cannot connect" in lower or "connection refused" in lower:
        return "Docker daemon is not running. Start Docker Desktop and wait until it shows Running."
    if "no space left" in lower or "disk full" in lower:
        return "Docker is out of disk space. Run `docker system prune -af` and free disk on the host."
    return error


def check_docker_available() -> Tuple[bool, str | None]:
    """Return (available, error_message). error_message is set when Docker is unavailable."""
    try:
        import docker
    except ImportError:
        return False, "Python docker package is not installed"

    try:
        client = docker.from_env()
        client.ping()
        return True, None
    except Exception as exc:
        return False, humanize_docker_error(str(exc))


def check_docker_smoke() -> Tuple[bool, str | None]:
    """
    Create and remove a tiny container. Catches storage/daemon issues that ping misses.
    """
    try:
        import docker
    except ImportError:
        return False, "Python docker package is not installed"

    try:
        client = docker.from_env()
        client.containers.run(
            "alpine:3.19",
            "echo band-smoke-ok",
            remove=True,
            detach=False,
        )
        return True, None
    except Exception as exc:
        return False, humanize_docker_error(str(exc))


def docker_unavailable_message(error: str | None) -> str:
    base = "Docker is required for reproduce and validate stages."
    if error:
        return f"{base} {humanize_docker_error(error)}"
    return base
