from __future__ import annotations

import asyncio
import difflib
import io
import json
import os
import shlex
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Callable, Literal 

from backend.agent_context import build_stage_prompt
from backend.alert_normalize import normalize_alert
from backend.alert_sanitize import public_alert
from backend.docker_health import (
    check_docker_available,
    check_docker_smoke,
    docker_unavailable_message,
    humanize_docker_error,
)
from backend.fix_export import export_validated_fix
from backend.git_output import push_fix_as_pr
from backend.repo_access import ensure_repo_checkout, load_repo_files, resolve_safe_repo_path
from pydantic import BaseModel

from backend.agent_names import AGENT_DISPLAY_NAMES, agent_mention
from backend.inference import GuardrailBlocked, InferenceClients
from backend.repo_stack import docker_fields_from_alert, enrich_alert_docker_from_repo
from backend.schemas import (
    AgentEvent,
    AgentHandoff,
    CandidatePatches,
    CodePatch,
    IncidentContext,
    IncidentState,
    PatchValidationResult,
    Provider,
    RCAReport,
    RawAlert,
    RegressionTests,
    ReproExecution,
    ReproPlan,
    Severity,
    Stage,
    ValidationSwarmResult,
    PatchResult,        
    ValidationReport,
)

Fallback = Callable[[IncidentState], BaseModel]


class FilePatch(BaseModel):
    """LLM output for a single file rewrite. We compute the diff ourselves."""
    file_path: str
    new_content: str
    summary: str
    rollback_plan: str


class FileRewriteCandidates(BaseModel):
    """LLM returns two file-rewrite candidates; we convert them to proper unified diffs."""
    candidates: list[FilePatch]

DEFAULT_CONTAINER_TIMEOUT_SECONDS = 300
MAX_CONTAINER_TIMEOUT_SECONDS = 600

# Skip heavy dirs when copying host repo into containers (especially on Windows bind mounts).
WORKSPACE_COPY_EXCLUDES: tuple[str, ...] = (
    "venv",
    ".venv",
    "node_modules",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".next",
    "dist",
    "build",
    ".cursor",
    "agent-transcripts",
)
MAX_VALIDATION_CANDIDATES = 2


def truncate_logs(log_string: str, max_lines: int = 200) -> str:
    """Return only the last max_lines from a Docker stdout/stderr string."""
    if max_lines <= 0:
        return ""
    return "\n".join(log_string.splitlines()[-max_lines:])


# 1. Move load_repo_files OUTSIDE the class as a standalone helper
def load_repo_files(repo_path: str, max_files: int = 20) -> dict:
    code_map = {}
    if not os.path.exists(repo_path):
        return code_map

    for root, _, files in os.walk(repo_path):
        for f in files:
            # Add other extensions if needed (e.g., .tsx, .rs, .md)
            if f.endswith((".py", ".ts", ".js", ".go", ".java")):
                full_path = os.path.join(root, f)
                try:
                    with open(full_path, "r", encoding="utf-8") as file:
                        # Store by relative path for cleaner LLM context
                        rel_path = os.path.relpath(full_path, repo_path)
                        rel_path = rel_path.replace("\\", "/")  # FORCE LINUX PATHS
                        code_map[rel_path] = file.read()
                except Exception:
                    continue

                if len(code_map) >= max_files:
                    return code_map

    return code_map

@dataclass(frozen=True)
class IncidentAgent:
    name: str
    mention: str
    stage: Stage
    provider: Provider
    output_model: type[BaseModel]
    system_prompt: str
    fallback: Fallback
    model_env: str
    default_model: str

    async def run(self, state: IncidentState, llm: InferenceClients) -> BaseModel:
        # Dump state to dict first
        state_dict = state.model_dump(mode="json")

        # Resolve repo path (fallback to alert payload if state isn't explicitly set)
        active_repo_path = state.repo_path or state.raw_alert.payload.get("repo_path")

       # INJECT REAL CODE: Only for agents that need to see code
        if active_repo_path and self.stage in {Stage.REPRO, Stage.TEST, Stage.FIX, Stage.VALIDATE}:
            state_dict["repository_files"] = load_repo_files(active_repo_path)

        # Include exact repro logs for RCA so it can cite specific line numbers and error types
        if self.stage == Stage.RCA and state.repro_execution:
            state_dict["repro_logs"] = state.repro_execution.logs

        prompt = json.dumps(state_dict, separators=(",", ":"))

        try:
            return await llm.json_call(
                provider=self.provider,
                model=self.model_name,
                system=self.system_prompt,
                user=prompt,
                output_model=self.output_model,
            )
        except GuardrailBlocked:
            return self.fallback(state)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            state.errors.append(f"{self.name}: {exc}")
            # Repo-aware safety: never let FIX fall back to placeholder diffs.
            if self.stage == Stage.FIX:
                raise
            return self.fallback(state)



    @property
    def model_name(self) -> str:
        return os.getenv(self.model_env) or self.default_model

@dataclass(frozen=True)
class DockerSandboxConfig:
    image: str = "python:3.11"
    repo_path: Path = field(default_factory=lambda: Path.cwd())
    workdir: str = "/workspace"
    source_mount: str = "/workspace_src"
    setup_command: str | None = None
    repro_command: str = "pytest"
    validation_command: str | None = None
    patch_strip: int = 1
    timeout_seconds: int = DEFAULT_CONTAINER_TIMEOUT_SECONDS
    network_disabled: bool = False
    build_command: str | None = None  # Explicit build command; None = auto-detect

    @classmethod
    def from_alert(
        cls,
        alert: dict[str, Any],
        tests: RegressionTests | None = None,
    ) -> "DockerSandboxConfig":
        timeout = min(
            max(
                1,
                _alert_int(
                    alert,
                    ("container_timeout_seconds", "docker_timeout_seconds"),
                    DEFAULT_CONTAINER_TIMEOUT_SECONDS,
                ),
            ),
            MAX_CONTAINER_TIMEOUT_SECONDS,
        )
        repo_path = Path(
            _alert_string(
                alert,
                ("repo_path", "repository_path", "workspace_path"),
                str(Path.cwd()),
            )
        ).expanduser()
        validation_command = _alert_string(
            alert,
            ("validation_command",),
            tests.run_command if tests else "pytest",
        )
        return cls(
            image=_alert_string(
                alert,
                ("docker_image", "container_image", "image"),
                "python:3.11",
            ),
            repo_path=repo_path.resolve(),
            workdir=_alert_string(alert, ("container_workdir", "workdir"), "/workspace"),
            source_mount=_alert_string(alert, ("source_mount",), "/workspace_src"),
            setup_command=_alert_optional_string(alert, ("setup_command", "docker_setup_command")),
            repro_command=_alert_string(
                alert,
                ("repro_command", "failing_command"),
                _alert_string(alert, ("test_command",), "pip install pytest && pytest"),
            ),
            validation_command=validation_command,
            patch_strip=max(0, _alert_int(alert, ("patch_strip",), 1)),
            timeout_seconds=max(1, timeout),
            network_disabled=_alert_bool(
                alert,
                ("docker_network_disabled", "network_disabled"),
                False,
            ),
            build_command=_alert_optional_string(alert, ("build_command",)),
        )


class DockerContainerExecutor:
    def __init__(self, config: DockerSandboxConfig, label: str) -> None:
        self.config = config
        self.label = label
        self.container: Any | None = None
        self._log_chunks: list[str] = []

    @property
    def logs_text(self) -> str:
        return "\n".join(chunk for chunk in self._log_chunks if chunk)

    def run_repro(self) -> ReproExecution:
        try:
            self._start_container()
            copy_code, _ = self._copy_workspace()
            if copy_code != 0:
                return self._repro_result(
                    exit_code=copy_code,
                    error="workspace copy failed before repro command",
                )

            setup_code = self._run_setup_if_needed()
            if setup_code not in (None, 0):
                return self._repro_result(
                    exit_code=setup_code,
                    error="setup command failed before repro command",
                )

            exit_code, _ = self._exec(
                "repro",
                self.config.repro_command,
                workdir=self.config.workdir,
            )
            return self._repro_result(exit_code=exit_code)
        except Exception as exc:
            self._log_chunks.append(f"[docker-error] {exc}")
            return self._repro_result(exit_code=None, error=str(exc))
        finally:
            self._cleanup_sync(kill=False)

    def run_validation(
        self,
        candidate_index: int,
        patch: CodePatch,
        tests: RegressionTests,
    ) -> PatchValidationResult:
        try:
            self._start_container()
            copy_code, _ = self._copy_workspace()
            if copy_code != 0:
                return self._validation_result(
                    candidate_index,
                    patch,
                    exit_code=copy_code,
                    error="workspace copy failed before validation",
                )

            setup_code = self._run_setup_if_needed()
            if setup_code not in (None, 0):
                return self._validation_result(
                    candidate_index,
                    patch,
                    exit_code=setup_code,
                    error="setup command failed before validation",
                )

            self._put_text_files(
                self.config.workdir,
                {_test_file_path(tests, self.config.workdir): tests.test_code},
            )
            self._put_text_files("/tmp", {"candidate.patch": patch.patch_unified_diff})

            patch_command = (
                "apt-get update -y && apt-get install -y patch || true; "
                f"cd {shlex.quote(self.config.workdir)} && "
                f"patch --batch --forward --fuzz=3 -p{self.config.patch_strip} "
                "-i /tmp/candidate.patch"
            )
            patch_code, patch_out = self._exec("patch", patch_command)
            if patch_code != 0:
                # Retry without fuzz (in case LLM generated exact-context diffs)
                patch_command2 = (
                    "apt-get update -y && apt-get install -y patch || true; "
                    f"cd {shlex.quote(self.config.workdir)} && "
                    f"patch --batch --forward -p{self.config.patch_strip} "
                    "-i /tmp/candidate.patch"
                )
                patch_code, _ = self._exec("patch-retry", patch_command2)
            if patch_code != 0:
                return self._validation_result(
                    candidate_index,
                    patch,
                    exit_code=patch_code,
                    error="patch command failed (tried --fuzz=3 and default fuzz)",
                )

            # --- Docker Build Pass ---
            # Detect the build command from the alert or repo stack, then run it.
            # This ensures the patched code actually compiles before claiming success.
            build_command = self.config.build_command or self._infer_build_command()
            if build_command:
                build_code, build_out = self._exec("build", build_command, workdir=self.config.workdir)
                if build_code != 0:
                    return self._validation_result(
                        candidate_index,
                        patch,
                        exit_code=build_code,
                        error=f"Docker build pass failed (fix introduces compile/syntax errors): {build_out[-500:]}",
                    )

            test_command = self.config.validation_command or tests.run_command
            exit_code, _ = self._exec("validation", test_command, workdir=self.config.workdir)
            return self._validation_result(candidate_index, patch, exit_code=exit_code)
        except Exception as exc:
            self._log_chunks.append(f"[docker-error] {exc}")
            return self._validation_result(candidate_index, patch, exit_code=None, error=str(exc))
        finally:
            self._cleanup_sync(kill=False)

    async def kill_and_remove(self) -> None:
        await asyncio.to_thread(self._cleanup_sync, True)

    def _start_container(self) -> None:
        if not self.config.repo_path.exists():
            raise FileNotFoundError(f"repo_path does not exist: {self.config.repo_path}")

        import docker

        client = docker.from_env()
        self.container = client.containers.run(
            self.config.image,
            command="sleep 3600",
            detach=True,
            working_dir="/",
            volumes={
                str(self.config.repo_path): {
                    "bind": self.config.source_mount,
                    "mode": "ro",
                }
            },
            network_disabled=self.config.network_disabled,
            labels={
                "band.incident_response": "true",
                "band.incident_response.job": self.label,
            },
        )
        self._log_chunks.append(f"[container-started] image={self.config.image} job={self.label}")

    def _copy_workspace(self) -> tuple[int, str]:
        workdir = self.config.workdir.rstrip("/") or "/workspace"
        source = self.config.source_mount.rstrip("/")
        exclude_flags = " ".join(
            f"--exclude={shlex.quote(name)}" for name in WORKSPACE_COPY_EXCLUDES
        )
        command = (
            f"rm -rf {shlex.quote(workdir)} && "
            f"mkdir -p {shlex.quote(workdir)} && "
            f"tar -C {shlex.quote(source)} {exclude_flags} -cf - . "
            f"| tar -xf - -C {shlex.quote(workdir)} && "
            f"find {shlex.quote(workdir)} -type f "
            f"-exec sed -i 's/\\r$//' {{}} + 2>/dev/null; true"
        )
        return self._exec("workspace-copy", command)

    def _run_setup_if_needed(self) -> int | None:
        if not self.config.setup_command:
            return None
        exit_code, _ = self._exec("setup", self.config.setup_command, workdir=self.config.workdir)
        return exit_code

    def _exec(self, label: str, command: str, workdir: str | None = None) -> tuple[int, str]:
        if self.container is None:
            raise RuntimeError("container has not been started")
        self._log_chunks.append(f"$ {command}")
        result = self.container.exec_run(["sh", "-lc", command], workdir=workdir, demux=True)
        output = _decode_exec_output(result.output)
        if output:
            self._log_chunks.append(output)
        exit_code = int(result.exit_code if result.exit_code is not None else -1)
        self._log_chunks.append(f"[exit_code={exit_code}] {label}")
        return exit_code, output

    def _infer_build_command(self) -> str | None:
        """Auto-detect the build/compile-check command based on repo structure."""
        workdir = shlex.quote(self.config.workdir)
        # Node.js with TypeScript — fastest compile check
        code, _ = self._exec("build-detect-ts", f"test -f {workdir}/tsconfig.json")
        if code == 0:
            return f"cd {workdir} && npx --yes tsc --noEmit 2>&1 | tail -30"
        # Node.js without TypeScript — just validate package.json exists
        code, _ = self._exec("build-detect-node", f"test -f {workdir}/package.json")
        if code == 0:
            return None  # No compile step for plain JS; skip build pass
        # Python project
        code, _ = self._exec("build-detect-py", f"test -f {workdir}/pyproject.toml || test -f {workdir}/setup.py")
        if code == 0:
            return f"cd {workdir} && python -m py_compile $(find . -name '*.py' -not -path '*/.*' | head -30) 2>&1"
        return None

    def _put_text_files(self, base_path: str, files: dict[str, str]) -> None:
        if self.container is None:
            raise RuntimeError("container has not been started")
        archive = _text_tar_archive(files)
        ok = self.container.put_archive(base_path, archive)
        if not ok:
            raise RuntimeError(f"failed to upload archive to {base_path}")
        self._log_chunks.append(f"[uploaded] {', '.join(files)} -> {base_path}")

    def _cleanup_sync(self, kill: bool) -> None:
        container = self.container
        if container is None:
            return
        try:
            if kill:
                try:
                    container.kill()
                    self._log_chunks.append("[container-killed]")
                except Exception as exc:
                    self._log_chunks.append(f"[container-kill-warning] {exc}")
            try:
                container.remove(force=True)
                self._log_chunks.append("[container-removed]")
            except Exception as exc:
                self._log_chunks.append(f"[container-remove-warning] {exc}")
        finally:
            self.container = None

    def _repro_result(self, *, exit_code: int | None, error: str | None = None) -> ReproExecution:
        logs = truncate_logs(self.logs_text)
        return ReproExecution(
            image=self.config.image,
            command=self.config.repro_command,
            exit_code=exit_code,
            failure_observed=(exit_code is not None and exit_code != 0 and error is None)
            or bool(error),
            logs=logs,
            stack_trace=logs,
            error=error,
        )

    def _validation_result(
        self,
        candidate_index: int,
        patch: CodePatch,
        *,
        exit_code: int | None,
        error: str | None = None,
    ) -> PatchValidationResult:
        return PatchValidationResult(
            candidate_index=candidate_index,
            validation_passed=exit_code == 0 and error is None,
            exit_code=exit_code,
            logs=truncate_logs(self.logs_text),
            error=error,
            patch_summary=patch.summary,
        )


async def run_repro_pass1(state: IncidentState, _plan: ReproPlan) -> ReproExecution:
    config = DockerSandboxConfig.from_alert(state.raw_alert.payload)
    available, docker_error = await asyncio.to_thread(check_docker_available)
    if not available:
        message = docker_unavailable_message(docker_error)
        return ReproExecution(
            image=config.image,
            command=config.repro_command,
            failure_observed=False,
            logs=message,
            stack_trace=message,
            error=message,
        )

    smoke_ok, smoke_error = await asyncio.to_thread(check_docker_smoke)
    if not smoke_ok:
        message = docker_unavailable_message(smoke_error)
        return ReproExecution(
            image=config.image,
            command=config.repro_command,
            failure_observed=False,
            logs=message,
            stack_trace=message,
            error=message,
        )

    executor = DockerContainerExecutor(config, f"repro-pass1-{state.run_id}")
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(executor.run_repro),
            timeout=config.timeout_seconds,
        )
    except asyncio.TimeoutError:
        await executor.kill_and_remove()
        logs = truncate_logs(
            f"{executor.logs_text}\n[timed-out] container exceeded {config.timeout_seconds}s"
        )
        return ReproExecution(
            image=config.image,
            command=config.repro_command,
            timed_out=True,
            failure_observed=False,
            logs=logs,
            stack_trace=logs,
            error=f"container exceeded {config.timeout_seconds}s timeout",
        )
    except Exception as exc:
        logs = truncate_logs(f"{executor.logs_text}\n[docker-error] {exc}")
        return ReproExecution(
            image=config.image,
            command=config.repro_command,
            failure_observed=False,
            logs=logs,
            stack_trace=logs,
            error=humanize_docker_error(str(exc)),
        )



def _make_unified_diff(file_path: str, original: str, new_content: str) -> str:
    """Generate a proper unified diff from original → new file content using difflib.

    Normalizes all line endings to Unix \\n before diffing so the result is
    safe to apply with `patch -p1` inside a Linux Docker container.
    """
    # Normalize to Unix line endings — critical for Linux patch command
    original_clean = original.replace("\r\n", "\n").replace("\r", "\n")
    new_clean = new_content.replace("\r\n", "\n").replace("\r", "\n")

    # splitlines(keepends=True) keeps the \n on each line.
    # Using lineterm="" on unified_diff would strip newlines from header lines
    # (---, +++, @@) causing them to merge with the next line.
    # Use the DEFAULT lineterm (adds \n to headers); content lines already have \n.
    original_lines = original_clean.splitlines(keepends=True)
    new_lines = new_clean.splitlines(keepends=True)

    safe_file_path = file_path.replace("\\", "/") # FIX PATH SEPARATORS
    diff_parts = difflib.unified_diff(
        original_lines,
        new_lines,
        fromfile=f"a/{safe_file_path}",
        tofile=f"b/{safe_file_path}",
        # No lineterm arg — use default (\n appended to header-only lines)
    )
    result = "".join(diff_parts)
    # Final CRLF normalization (shouldn't be needed but be safe)
    return result.replace("\r\n", "\n").replace("\r", "\n")





def _docker_verify_patch(
    repo_path: str,
    docker_image: str,
    setup_command: str,
    file_path: str,
    patch_diff: str,
) -> tuple[bool, str]:
    """
    Spin up a Docker container, apply the patch, and return (success, logs).
    This pre-flight check catches malformed diffs before the full Validation Swarm.
    """
    import docker as docker_module

    logs: list[str] = []
    try:
        from pathlib import Path
        abs_repo_path = str(Path(repo_path).resolve())
        client = docker_module.from_env()
        container = client.containers.run(
            docker_image,
            command="sleep 120",
            detach=True,
            working_dir="/",
            volumes={abs_repo_path: {"bind": "/workspace_src", "mode": "ro"}},
            labels={"band.incident_response": "true", "band.preflight": "true"},
        )

        try:
            # Copy workspace
            exclude_flags = " ".join(
                f"--exclude={shlex.quote(name)}" for name in WORKSPACE_COPY_EXCLUDES
            )
            copy_cmd = (
                "rm -rf /workspace && mkdir -p /workspace && "
                f"tar -C /workspace_src {exclude_flags} -cf - . "
                "| tar -xf - -C /workspace && "
                "find /workspace -type f -exec sed -i 's/\\r$//' {} + 2>/dev/null; true"
            )
            result = container.exec_run(["sh", "-lc", copy_cmd], workdir="/")
            logs.append(f"[copy] exit={result.exit_code}")
            if result.exit_code != 0:
                return False, "\n".join(logs)

            # Run setup
            if setup_command:
                result = container.exec_run(
                    ["sh", "-lc", setup_command], workdir="/workspace"
                )
                logs.append(f"[setup] exit={result.exit_code}")
                out = result.output.decode("utf-8", errors="replace") if result.output else ""
                if out:
                    logs.append(out[-2000:])
                if result.exit_code != 0:
                    return False, "\n".join(logs)

            # Upload and apply patch
            import io as _io, tarfile as _tarfile
            buf = _io.BytesIO()
            with _tarfile.open(fileobj=buf, mode="w") as tar:
                enc = patch_diff.encode("utf-8")
                info = _tarfile.TarInfo(name="candidate.patch")
                info.size = len(enc)
                tar.addfile(info, _io.BytesIO(enc))
            buf.seek(0)
            container.put_archive("/tmp", buf.read())

            patch_cmd = (
                "apt-get update -y && apt-get install -y patch || true; "
                "cd /workspace && "
                "patch --batch --forward --fuzz=3 -p1 -i /tmp/candidate.patch"
            )
            result = container.exec_run(["sh", "-lc", patch_cmd], workdir="/workspace")
            out = result.output.decode("utf-8", errors="replace") if result.output else ""
            logs.append(f"[patch] exit={result.exit_code}")
            if out:
                logs.append(out[-1000:])
            return result.exit_code == 0, "\n".join(logs)
        finally:
            try:
                container.remove(force=True)
            except Exception:
                pass
    except Exception as exc:
        logs.append(f"[docker-preflight-error] {exc}")
        return False, "\n".join(logs)


async def _build_verified_patches(
    state: IncidentState,
    llm: "InferenceClients",
) -> CandidatePatches:
    """
    Ask the LLM to rewrite specific files (not generate a unified diff).
    We then compute real diffs using difflib and Docker-verify each one applies.
    Falls back to _fallback_fix if LLM or Docker fails.
    """
    from backend.agent_context import build_stage_prompt, select_relevant_files, _extract_keywords

    alert = state.raw_alert.payload
    keywords = _extract_keywords(state)
    relevant_files = select_relevant_files(state.repo_files, keywords, max_chars=14_000)
    repo_path = state.repo_path or alert.get("repo_path", "")
    docker_image = alert.get("docker_image", "node:20-bookworm-slim")
    setup_command = alert.get("setup_command", "")

    # Build a file manifest so the LLM knows exactly what paths exist
    # Build manifest with file sizes to anchor the LLM
    file_manifest_lines = [
        f"  - {p} ({len(c)} chars)"
        for p, c in sorted(state.repo_files.items())
    ]
    file_manifest = "\n".join(file_manifest_lines)

    system = (
        "You are a principal software engineer performing a surgical, production-safe fix for an incident.\n\n"

        "=== MANDATORY PRE-ANALYSIS (complete ALL steps before writing any code) ===\n"
        "Before writing any fix, you MUST internally answer the following questions by reading the repo_files:\n"
        "  A. What is the user ACTUALLY complaining about in plain English? (not what they literally typed — infer intent)\n"
        "  B. Which exact file(s) in available_files are responsible for the UI or logic the user is seeing?\n"
        "  C. What exact change in those files will produce the behavior the user expects?\n"
        "  D. Cross-check the triage context 'interpretations' and 'investigation_plan'. Does your fix address all of them?\n"
        "  E. Is there a simpler, safer fix that avoids touching unrelated code? Prefer minimal, targeted edits.\n\n"

        "=== ABSOLUTE RULES (violating any rule means the patch is REJECTED) ===\n"
        "1. candidates: EXACTLY 2 fix candidates. Each candidate must target a DIFFERENT file approach.\n"
        "2. file_path: MUST be a path copied character-for-character from 'available_files'. "
        "   NO invented paths. NO guessed filenames. ZERO tolerance for hallucinated paths.\n"
        "3. new_content: the COMPLETE rewritten file content — not a diff, not a partial snippet. "
        "   Base it on the original content from repo_files and apply the minimal targeted change.\n"
        "4. NEVER edit README.md or documentation files as the 'fix'. "
        "   The fix MUST be in actual source code (e.g., .ts, .tsx, .py, .js, .jsx).\n"
        "5. summary: one sentence describing exactly what changed and why.\n"
        "6. rollback_plan: one sentence describing how to undo this change safely.\n"
    )

    user_payload = {
        "stage": "fix",
        "service": alert.get("service_short") or alert.get("service"),
        "repo_full_name": state.repo_full_name,
        "repo_stack": alert.get("repo_stack", "unknown"),
        "incident": {
            "error": alert.get("error"),
            "impact": alert.get("impact"),
            "context": state.context.model_dump(mode="json") if state.context else None,
            "repro_logs": (state.repro_execution.logs[-2000:] if state.repro_execution else ""),
        },
        "available_files": sorted(state.repo_files.keys()),
        "repo_files": relevant_files,
    }

    try:
        rewrites: FileRewriteCandidates = await llm.json_call(
            provider=Provider.FEATHERLESS,
            model=state.raw_alert.payload.get("patch_model") or "Qwen/Qwen2.5-Coder-32B-Instruct",
            system=system,
            user=json.dumps(user_payload, separators=(",", ":")),
            output_model=FileRewriteCandidates,
        )
    except Exception as exc:
        state.errors.append(f"patch-rewrite-llm: {exc}")
        return _fallback_fix(state)

    candidates: list[CodePatch] = []
    for fp in rewrites.candidates:
        # Validate file path is real
        if fp.file_path not in state.repo_files:
            # Try case-insensitive match
            matches = [k for k in state.repo_files if k.lower() == fp.file_path.lower()]
            if matches:
                fp = fp.model_copy(update={"file_path": matches[0]})
            else:
                state.errors.append(
                    f"patch-rewrite: LLM specified unknown file '{fp.file_path}', skipping"
                )
                continue

        original = state.repo_files[fp.file_path]
        diff = _make_unified_diff(fp.file_path, original, fp.new_content)

        if not diff.strip():
            state.errors.append(f"patch-rewrite: LLM made no changes to '{fp.file_path}'")
            continue

        # Docker pre-flight verify
        if repo_path:
            ok, logs = await asyncio.to_thread(
                _docker_verify_patch, repo_path, docker_image, setup_command, fp.file_path, diff
            )
            if not ok:
                state.errors.append(
                    f"patch-preflight: diff for '{fp.file_path}' failed Docker pre-check:\n{logs[-500:]}"
                )
                # Still include it — Validation Swarm will do a full test run
                # But log so we know
        else:
            ok = True  # No repo path, can't pre-check

        candidates.append(
            CodePatch(
                summary=fp.summary,
                files_changed=[fp.file_path],
                patch_unified_diff=diff,
                risk_notes=[] if ok else ["Docker pre-flight check failed — patch may not apply cleanly"],
                rollback_plan=fp.rollback_plan,
            )
        )

    if len(candidates) < 2:
        # Pad with fallback if LLM didn't give us two valid candidates
        fallback = _fallback_fix(state)
        while len(candidates) < 2 and fallback.candidates:
            candidates.append(fallback.candidates[len(candidates)])

    # Ensure candidates are distinct
    if len(candidates) >= 2 and candidates[0].patch_unified_diff == candidates[1].patch_unified_diff:
        # Make second one trivially different (add a comment)
        first_path = candidates[0].files_changed[0] if candidates[0].files_changed else "unknown"
        original = state.repo_files.get(first_path, "")
        diff2 = _make_unified_diff(
            first_path, original, candidates[1].new_content if hasattr(candidates[1], "new_content") else original
        )
        candidates[1] = candidates[1].model_copy(update={"patch_unified_diff": diff2 or candidates[0].patch_unified_diff + " "})

    return CandidatePatches(candidates=candidates[:2])


async def run_validation_swarm(

    state: IncidentState,
    patches: CandidatePatches,
    tests: RegressionTests,
) -> ValidationSwarmResult:
    config = DockerSandboxConfig.from_alert(state.raw_alert.payload, tests)
    available, docker_error = await asyncio.to_thread(check_docker_available)
    if not available:
        message = docker_unavailable_message(docker_error)
        return ValidationSwarmResult(
            results=[
                PatchValidationResult(
                    candidate_index=0,
                    validation_passed=False,
                    logs=message,
                    error=message,
                    patch_summary=patches.candidates[0].summary if patches.candidates else "",
                )
            ]
        )

    smoke_ok, smoke_error = await asyncio.to_thread(check_docker_smoke)
    if not smoke_ok:
        message = docker_unavailable_message(smoke_error)
        return ValidationSwarmResult(
            results=[
                PatchValidationResult(
                    candidate_index=0,
                    validation_passed=False,
                    logs=message,
                    error=message,
                    patch_summary=patches.candidates[0].summary if patches.candidates else "",
                )
            ]
        )

    candidates = patches.candidates[:MAX_VALIDATION_CANDIDATES]
    task_to_job: dict[
        asyncio.Task[PatchValidationResult],
        tuple[DockerContainerExecutor, int, CodePatch],
    ] = {}

    for index, patch in enumerate(candidates):
        executor = DockerContainerExecutor(config, f"validation-{state.run_id}-{index}")
        task = asyncio.create_task(_run_validation_job(executor, index, patch, tests))
        task_to_job[task] = (executor, index, patch)

    pending: set[asyncio.Task[PatchValidationResult]] = set(task_to_job)
    results: list[PatchValidationResult] = []

    try:
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                executor, index, patch = task_to_job[task]
                try:
                    result = task.result()
                except Exception as exc:
                    result = PatchValidationResult(
                        candidate_index=index,
                        validation_passed=False,
                        logs=truncate_logs(f"{executor.logs_text}\n[validation-error] {exc}"),
                        error=str(exc),
                        patch_summary=patch.summary,
                    )
                results.append(result)

                if result.validation_passed:
                    results.extend(await _terminate_pending_validations(pending, task_to_job))
                    return ValidationSwarmResult(
                        winning_candidate_index=index,
                        winning_patch=patch,
                        results=sorted(results, key=lambda item: item.candidate_index),
                    )

        return ValidationSwarmResult(results=sorted(results, key=lambda item: item.candidate_index))
    finally:
        await asyncio.gather(
            *(executor.kill_and_remove() for executor, _, _ in task_to_job.values()),
            return_exceptions=True,
        )


async def _run_validation_job(
    executor: DockerContainerExecutor,
    candidate_index: int,
    patch: CodePatch,
    tests: RegressionTests,
) -> PatchValidationResult:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(executor.run_validation, candidate_index, patch, tests),
            timeout=executor.config.timeout_seconds,
        )
    except asyncio.TimeoutError:
        await executor.kill_and_remove()
        return PatchValidationResult(
            candidate_index=candidate_index,
            validation_passed=False,
            timed_out=True,
            logs=truncate_logs(
                f"{executor.logs_text}\n"
                f"[timed-out] container exceeded {executor.config.timeout_seconds}s"
            ),
            error=f"container exceeded {executor.config.timeout_seconds}s timeout",
            patch_summary=patch.summary,
        )


async def _terminate_pending_validations(
    pending: set[asyncio.Task[PatchValidationResult]],
    task_to_job: dict[
        asyncio.Task[PatchValidationResult],
        tuple[DockerContainerExecutor, int, CodePatch],
    ],
) -> list[PatchValidationResult]:
    if not pending:
        return []

    cancelled_results: list[PatchValidationResult] = []
    for task in pending:
        executor, index, patch = task_to_job[task]
        cancelled_results.append(
            PatchValidationResult(
                candidate_index=index,
                validation_passed=False,
                logs=truncate_logs(executor.logs_text),
                error="stopped after another candidate passed validation",
                patch_summary=patch.summary,
            )
        )

    await asyncio.gather(
        *(task_to_job[task][0].kill_and_remove() for task in pending),
        return_exceptions=True,
    )
    _, still_pending = await asyncio.wait(pending, timeout=5)
    for task in still_pending:
        task.cancel()
    if still_pending:
        await asyncio.gather(*still_pending, return_exceptions=True)
    return cancelled_results


def _pipeline_steps() -> list[dict[str, str]]:
    return [
        {"stage": "triage", "label": "Triage alert"},
        {"stage": "repro", "label": "Reproduce in Docker"},
        {"stage": "test", "label": "Write regression test"},
        {"stage": "fix", "label": "Generate fix candidates"},
        {"stage": "validate", "label": "Validate patches in Docker"},
        {"stage": "rca", "label": "Publish RCA report"},
        {"stage": "push", "label": "Push branch and open PR"},
    ]


def _pipeline_agents() -> list[dict[str, str]]:
    return [
        {
            "name": AGENT_DISPLAY_NAMES[Stage.TRIAGE],
            "mention": agent_mention(Stage.TRIAGE),
            "stage": Stage.TRIAGE.value,
            "kind": "band",
        },
        {
            "name": AGENT_DISPLAY_NAMES[Stage.REPRO],
            "mention": agent_mention(Stage.REPRO),
            "stage": Stage.REPRO.value,
            "kind": "band",
        },
        {
            "name": "Repro Sandbox",
            "mention": "@repro-sandbox",
            "stage": Stage.REPRO.value,
            "kind": "infrastructure",
        },
        {
            "name": AGENT_DISPLAY_NAMES[Stage.TEST],
            "mention": agent_mention(Stage.TEST),
            "stage": Stage.TEST.value,
            "kind": "band",
        },
        {
            "name": AGENT_DISPLAY_NAMES[Stage.FIX],
            "mention": agent_mention(Stage.FIX),
            "stage": Stage.FIX.value,
            "kind": "band",
        },
        {
            "name": "Validation Swarm",
            "mention": "@validation-swarm",
            "stage": Stage.VALIDATE.value,
            "kind": "infrastructure",
        },
        {
            "name": AGENT_DISPLAY_NAMES[Stage.RCA],
            "mention": agent_mention(Stage.RCA),
            "stage": Stage.RCA.value,
            "kind": "band",
        },
        {
            "name": "Orchestrator",
            "mention": "@orchestrator",
            "stage": "orchestrator",
            "kind": "infrastructure",
        },
    ]


class IncidentOrchestrator:
    transitions = {
        Stage.TRIAGE: Stage.REPRO,
        Stage.REPRO: Stage.TEST,
        Stage.TEST: Stage.FIX,
        Stage.FIX: Stage.VALIDATE,
        Stage.VALIDATE: Stage.RCA,
        Stage.RCA: Stage.DONE,
    }

    def __init__(self, llm: InferenceClients | None = None) -> None:
        self.llm = llm or InferenceClients()
        self.agents = build_agents()

    async def run(self, alert: dict[str, Any]) -> AsyncIterator[AgentEvent]:
        # Extract repo context immediately
        repo_path = alert.get("repo_path", "")
        # Fallback to service name if full name isn't provided, to prevent crashes
        repo_full_name = alert.get("repo_full_name") or alert.get("service", "unknown/unknown")

        state = IncidentState(
            raw_alert=RawAlert(payload=alert),
            repo_path=repo_path,
            repo_full_name=repo_full_name,
        )
        yield self._event(state, Stage.TRIAGE, "orchestrator", "queued", {"alert": alert})

        # ==========================================
        # ADD THIS BLOCK HERE TO FIX THE CLONE ERROR
        # ==========================================
        await self._prepare_repository(state, alert)
        if state.current_stage == Stage.FAILED:
            async for event in self._finalize_run(state, alert):
                yield event
            return
        # ==========================================

        while state.current_stage not in {Stage.DONE, Stage.FAILED}:

            if state.steps_run >= state.max_steps:
                state.current_stage = Stage.FAILED
                state.errors.append("max_steps exceeded")
                async for event in self._finalize_run(state, alert):
                    yield event
                return

            if state.current_stage == Stage.VALIDATE:
                async for event in self._run_validation_stage(state):
                    yield event
                continue

            agent = self.agents[state.current_stage]
            yield self._event(state, agent.stage, agent.name, "active", self._stage_payload(state))

            # FIX stage: use Docker-verified file-rewrite approach instead of raw diff generation
            if agent.stage == Stage.FIX:
                output = await _build_verified_patches(state, self.llm)
                state.candidate_patches = output
            else:
                output = await agent.run(state, self.llm)
                self._merge_output(state, agent.stage, output)


            if agent.stage == Stage.REPRO:
                repro_config = DockerSandboxConfig.from_alert(state.raw_alert.payload)
                self._add_handoff(
                    state,
                    from_agent=agent.name,
                    to_agent="Repro Sandbox",
                    stage=Stage.REPRO,
                    mention="@repro-sandbox",
                    payload=output.model_dump(mode="json"),
                    summary="Repro plan handed to Docker sandbox for Pass 1 execution.",
                )
                yield self._event(
                    state,
                    Stage.REPRO,
                    agent.name,
                    "handoff",
                    {
                        "mention": "@repro-sandbox",
                        "from_agent": agent.name,
                        "to_agent": "Repro Sandbox",
                        "summary": "Repro plan handed to Docker sandbox for Pass 1 execution.",
                        "payload": output.model_dump(mode="json"),
                    },
                )
                yield self._event(
                    state,
                    Stage.REPRO,
                    "Repro Sandbox",
                    "active",
                    {"timeout_seconds": repro_config.timeout_seconds},
                )
                state.repro_execution = await run_repro_pass1(
                    state,
                    output,  # type: ignore[arg-type]
                )
                repro_status = "complete"
                if state.repro_execution.error:
                    repro_status = "failed"
                    state.errors.append(f"repro: {state.repro_execution.error}")
                    state.current_stage = Stage.FAILED
                yield self._event(
                    state,
                    Stage.REPRO,
                    "Repro Sandbox",
                    repro_status,  # type: ignore[arg-type]
                    state.repro_execution.model_dump(mode="json"),
                    error=state.repro_execution.error,
                )
                if state.current_stage == Stage.FAILED:
                    async for event in self._finalize_run(state, alert):
                        yield event
                    return

            state.steps_run += 1
            next_stage = self.transitions[agent.stage]
            output_payload = self._output_payload(state, agent.stage, output)
            handoff_from = agent.name
            if agent.stage == Stage.REPRO:
                handoff_from = "Repro Sandbox"
            self._add_handoff_if_needed(state, handoff_from, next_stage, output_payload)
            yield self._event(state, agent.stage, agent.name, "complete", output_payload)

            if next_stage != Stage.DONE:
                next_agent_name, next_mention = self._next_agent_metadata(next_stage)
                yield self._event(
                    state,
                    next_stage,
                    agent.name,
                    "handoff",
                    {
                        "mention": next_mention,
                        "from_agent": agent.name,
                        "to_agent": next_agent_name,
                        "summary": (
                            f"{agent.name} handed structured incident state to {next_agent_name}."
                        ),
                        "payload": self._stage_payload(state),
                    },
                )

            state.current_stage = next_stage

        async for event in self._finalize_run(state, alert):
            yield event

    async def _finalize_run(
        self,
        state: IncidentState,
        alert: dict[str, Any],
    ) -> AsyncIterator[AgentEvent]:
        if state.current_stage == Stage.FAILED:
            if state.fix and not state.fix_export:
                state.fix_export = await asyncio.to_thread(export_validated_fix, state)
            yield self._event(
                state,
                Stage.FAILED,
                "orchestrator",
                "failed",
                {
                    "errors": state.errors,
                    "repo_full_name": state.repo_full_name,
                    "repo_path": state.repo_path,
                    "fix": state.fix.model_dump(mode="json") if state.fix else None,
                    "fix_export": state.fix_export,
                    "tests": state.tests.model_dump(mode="json") if state.tests else None,
                    "rca": state.rca.model_dump(mode="json") if state.rca else None,
                },
                error="; ".join(state.errors) if state.errors else "pipeline failed",
            )
            return

        if state.fix:
            state.fix_export = await asyncio.to_thread(export_validated_fix, state)

        pr_url: str | None = None
        pr_error: str | None = None
        auto_pr = bool(alert.get("auto_pr"))

        if auto_pr and state.rca and state.rca.patch_unified_diff and state.fix:
            try:
                from backend.git_output import push_fix_as_pr
                # Use the consistently set repo_path from the state
                pr_url = await push_fix_as_pr(state=state, repo_path=state.repo_path)
            except Exception as exc:
                pr_error = str(exc)
                state.errors.append(f"pr: {exc}")
        elif state.rca and state.rca.patch_unified_diff and not auto_pr:
            pr_error = "auto_pr disabled; patch available in RCA only"
        elif auto_pr and not state.fix:
            pr_error = "no validated fix to push"

        yield self._event(
            state,
            Stage.DONE,
            "orchestrator",
            "done",
            {
                "rca": state.rca.model_dump(mode="json") if state.rca else None,
                "fix": state.fix.model_dump(mode="json") if state.fix else None,
                "validation": (
                    state.validation.model_dump(mode="json") if state.validation else None
                ),
                "repo_full_name": state.repo_full_name,
                "branch": state.rca.git_branch if state.rca else None,
                "pr_url": pr_url,
                "pr_error": pr_error,
                "errors": state.errors,
                "fix_export": state.fix_export,
            },
        )

    async def _prepare_repository(self, state: IncidentState, alert: dict[str, Any]) -> None:
        repo_path, repo_error = await ensure_repo_checkout(alert)
        state.repo_path = repo_path or None
        state.repo_full_name = alert.get("repo_full_name")
        if repo_path:
            alert["repo_path"] = repo_path
        if repo_error:
            state.errors.append(f"repo: {repo_error}")
            if alert.get("repo_url"):
                state.current_stage = Stage.FAILED
            return
        try:
            resolved = resolve_safe_repo_path(repo_path)
            state.repo_files = await asyncio.to_thread(load_repo_files, resolved)
            enrich_alert_docker_from_repo(alert, resolved)
            state.raw_alert.payload.update(docker_fields_from_alert(alert))
        except Exception as exc:
            state.errors.append(f"repo_files: {exc}")

    async def _run_validation_stage(self, state: IncidentState) -> AsyncIterator[AgentEvent]:
        yield self._event(
            state,
            Stage.VALIDATE,
            "Validation Swarm",
            "active",
            self._stage_payload(state),
        )
        if state.candidate_patches is None or state.tests is None:
            state.current_stage = Stage.FAILED
            state.errors.append("validation: missing candidate patches or regression tests")
            yield self._event(
                state,
                Stage.FAILED,
                "Validation Swarm",
                "failed",
                error="validation requires candidate patches and regression tests",
            )
            return

        if not state.candidate_patches.candidates:
            state.current_stage = Stage.FAILED
            state.errors.append(
                "validation: no candidate patches to validate — enable LIVE_LLM_ENABLED "
                "for real GitHub repositories"
            )
            yield self._event(
                state,
                Stage.FAILED,
                "Validation Swarm",
                "failed",
                error="no candidate patches to validate",
            )
            return

        state.validation = await run_validation_swarm(state, state.candidate_patches, state.tests)
        state.fix = state.validation.winning_patch
        state.steps_run += 1

        if state.fix is None:
            state.current_stage = Stage.FAILED
            state.errors.append("validation: no candidate patch passed validation")
            yield self._event(
                state,
                Stage.FAILED,
                "Validation Swarm",
                "failed",
                state.validation.model_dump(mode="json"),
                error="no candidate patch passed validation",
            )
            return

        next_stage = self.transitions[Stage.VALIDATE]
        self._add_handoff_if_needed(
            state,
            "Validation Swarm",
            next_stage,
            state.validation.model_dump(mode="json"),
        )
        yield self._event(
            state,
            Stage.VALIDATE,
            "Validation Swarm",
            "complete",
            state.validation.model_dump(mode="json"),
        )
        next_agent_name, next_mention = self._next_agent_metadata(next_stage)
        yield self._event(
            state,
            next_stage,
            "Validation Swarm",
            "handoff",
            {
                "mention": next_mention,
                "from_agent": "Validation Swarm",
                "to_agent": next_agent_name,
                "summary": f"Validation Swarm handed validated fix to {next_agent_name}.",
                "payload": self._stage_payload(state),
            },
        )
        state.current_stage = next_stage

    def _merge_output(self, state: IncidentState, stage: Stage, output: BaseModel) -> None:
        if stage == Stage.TRIAGE:
            state.context = output  # type: ignore[assignment]
        elif stage == Stage.REPRO:
            state.repro = output  # type: ignore[assignment]
        elif stage == Stage.TEST:
            state.tests = output  # type: ignore[assignment]
        elif stage == Stage.FIX:
            state.candidate_patches = output  # type: ignore[assignment]
        elif stage == Stage.RCA:
            state.rca = output  # type: ignore[assignment]

    def _add_handoff_if_needed(
        self,
        state: IncidentState,
        from_agent: str,
        next_stage: Stage,
        payload: dict[str, Any],
    ) -> None:
        if next_stage == Stage.DONE:
            return
        next_agent_name, next_mention = self._next_agent_metadata(next_stage)
        self._add_handoff(
            state,
            from_agent=from_agent,
            to_agent=next_agent_name,
            stage=next_stage,
            mention=next_mention,
            payload=payload,
            summary=f"{from_agent} handed structured incident state to {next_agent_name}.",
        )

    def _add_handoff(
        self,
        state: IncidentState,
        *,
        from_agent: str,
        to_agent: str,
        stage: Stage,
        mention: str,
        payload: dict[str, Any],
        summary: str,
    ) -> None:
        state.band_thread.append(
            AgentHandoff(
                from_agent=from_agent,
                to_agent=to_agent,
                stage=stage,
                mention=mention,
                payload=payload,
                summary=summary,
            )
        )

    def _next_agent_metadata(self, stage: Stage) -> tuple[str, str]:
        if stage == Stage.VALIDATE:
            return "Validation Swarm", "@validation-swarm"
        agent = self.agents[stage]
        return agent.name, agent.mention

    def _event(
        self,
        state: IncidentState,
        stage: Stage,
        agent: str,
        status: str,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> AgentEvent:
        event = AgentEvent(
            run_id=state.run_id,
            stage=stage,
            agent=agent,
            status=status,  # type: ignore[arg-type]
            payload=payload or {},
            error=error,
        )
        state.events.append(event)
        return event

    def _stage_payload(self, state: IncidentState) -> dict[str, Any]:
        return {
            "context": state.context.model_dump(mode="json") if state.context else None,
            "repro": state.repro.model_dump(mode="json") if state.repro else None,
            "repro_execution": (
                state.repro_execution.model_dump(mode="json") if state.repro_execution else None
            ),
            "tests": state.tests.model_dump(mode="json") if state.tests else None,
            "candidate_patches": (
                state.candidate_patches.model_dump(mode="json") if state.candidate_patches else None
            ),
            "fix": state.fix.model_dump(mode="json") if state.fix else None,
            "validation": state.validation.model_dump(mode="json") if state.validation else None,
            "errors": state.errors,
        }

    def _output_payload(
        self,
        state: IncidentState,
        stage: Stage,
        output: BaseModel,
    ) -> dict[str, Any]:
        payload = output.model_dump(mode="json")
        if stage == Stage.REPRO and state.repro_execution:
            payload["repro_execution"] = state.repro_execution.model_dump(mode="json")
        return payload
    
    


def build_agents() -> dict[Stage, IncidentAgent]:
    return {
        Stage.TRIAGE: IncidentAgent(
            name=AGENT_DISPLAY_NAMES[Stage.TRIAGE],
            mention=agent_mention(Stage.TRIAGE),
            stage=Stage.TRIAGE,
            provider=Provider.AIML,
            model_env="TRIAGE_MODEL",
            default_model="gpt-4o-mini",
            output_model=IncidentContext,
            system_prompt=(
                "You are an expert on-call incident triager at a high-traffic engineering team.\n\n"
                "STEP 1 — UNDERSTAND THE USER (most important step):\n"
                "The 'error' field is a free-text description written by a non-technical user who may not know "
                "how to articulate a software bug clearly. Your first job is to decode their INTENT. "
                "Ask yourself: 'What is this person frustrated about? What behavior did they expect vs. what do they see?' "
                "Use the repository source files provided to verify which components match that description.\n\n"
                "STEP 2 — CLASSIFY:\n"
                "Your ONLY output is a valid JSON object matching the IncidentContext schema — nothing else.\n"
                "1. service: use the leaf service name, not the org/repo path (e.g. 'checkout', not 'acme/checkout').\n"
                "2. environment: must be one of production, staging, ci, demo, or unknown.\n"
                "3. severity: classify as sev1 (complete outage), sev2 (partial degradation), sev3 (minor), or sev4 (cosmetic).\n"
                "4. error_signature: a short unique identifier for the error class (e.g. 'missing-test-credentials-on-login-ui').\n"
                "5. impact: one sentence describing the customer-facing effect.\n"
                "6. suspected_components: search the repo_files for the UI component or function that would need to change. "
                "   List the EXACT file paths that exist in the repository (not guessed paths).\n"
                "7. evidence: list 3-5 key facts from the user description and repo files that support your analysis.\n"
                "8. interpretations: list 2-3 different perspectives on what the user could mean — consider both UI and data/config interpretations.\n"
                "9. investigation_plan: a step-by-step plan for the Patch Generator agent to fix the code without hallucinating paths.\n"
                "10. NEVER invent stack traces, error messages, or file paths not present in the repo_files.\n"
                "11. If a field cannot be determined, use null — never guess."
            ),
            fallback=_fallback_triage,
        ),
        Stage.REPRO: IncidentAgent(
            name=AGENT_DISPLAY_NAMES[Stage.REPRO],
            mention=agent_mention(Stage.REPRO),
            stage=Stage.REPRO,
            provider=Provider.FEATHERLESS,
            model_env="REPRO_MODEL",
            default_model="Qwen/Qwen2.5-Coder-32B-Instruct",
            output_model=ReproPlan,
            system_prompt=(
                "You are a senior SRE designing a deterministic Docker reproduction of a production incident. "
                "You receive a structured IncidentContext and a snapshot of the repository source files. "
                "Your ONLY output is a valid JSON object matching the ReproPlan schema — nothing else. "
                "Rules: "
                "1. repro_command: a single shell command that reliably triggers the reported failure. "
                "   It must be runnable inside the Docker container at /workspace with no external network calls. "
                "   Prefer: 'python -c \"from <module> import <fn>; <fn>(<bad_input>)\"' for Python. "
                "2. expected_exit_code: the exit code the repro command will produce (usually 1 for exceptions). "
                "3. expected_failure: the exact exception type and message you expect (e.g. 'TypeError: ...'). "
                "4. steps: list 3-6 ordered English sentences describing what happens during repro. "
                "5. files_involved: list only the source files directly implicated by the stack trace. "
                "6. environment_vars: list any env vars the repro command needs (usually empty for unit-level repros). "
                "7. If you cannot determine the repro command with confidence, set repro_command to 'echo CANNOT_REPRO' "
                "   and explain in steps why the information is insufficient."
            ),
            fallback=_fallback_repro,
        ),
        Stage.TEST: IncidentAgent(
            name=AGENT_DISPLAY_NAMES[Stage.TEST],
            mention=agent_mention(Stage.TEST),
            stage=Stage.TEST,
            provider=Provider.FEATHERLESS,
            model_env="REGRESSION_TEST_MODEL",
            default_model="Qwen/Qwen2.5-Coder-32B-Instruct",
            output_model=RegressionTests,
            system_prompt=(
                "You are an expert Test Automation Architect. Your task is to write robust, self-discovering regression tests.\n"
                "1. ANALYZE MODULES: Before writing tests, inspect the `repo_files` to determine the module structure. "
                "   Use `PYTHONPATH=.` to ensure the root directory is importable.\n"
                "2. DYNAMIC DISCOVERY: Do not hardcode tests for specific functions if the module exposes multiple entry points. "
                "   If you need to verify variable states, explicitly import them from the target module.\n"
                "3. STDOUT CAPTURE: To test print() output in Python, you MUST pass `capsys` as a parameter to the test function. "
                "   CORRECT PATTERN:\n"
                "   def test_func(capsys):\n"
                "       func_to_test()\n"
                "       captured = capsys.readouterr()\n"
                "       assert 'expected' in captured.out\n"
                "   NEVER use `pytest.capture` or `pytest.capture.capsys()`. These do not exist.\n"
                "4. GENERIC IMPORTING: When importing, generate the import path based on the directory structure. "
                "   If file is at `level_1_syntax/app.py`, import via `from level_1_syntax.app import ...`.\n"
                "5. ROBUSTNESS: Ensure tests handle the `SyntaxError` case by checking if the module can be imported. "
                "6. FINAL VALIDATION: Before outputting JSON, perform a mental check: "
                "   a) Is `capsys` in the function parentheses? (Must be: def test_func(capsys):). "
                "   b) Did I import every variable/function used? (e.g., 'from module import variable'). "
                "   c) Is the module path correct relative to root? (PYTHONPATH=. makes the root the base)."
                "7. run_command: the exact single shell command that runs the test (e.g. 'pip install pytest && PYTHONPATH=. pytest tests/test_regression.py')."
                "8. CONTEXT-AWARE TESTING: ONLY write tests for files and functions that actually exist in the `repo_files` provided. "
                "   - Do NOT assume a database exists. "
                "   - Do NOT assume filesystem paths like '/app/uploads/' exist. "
                "   - If a module (like level_4_security) is not in `repo_files`, do NOT import or test it. "
                "   - Focus ONLY on testing the code that is actually present in the provided directory."
            ),
            fallback=_fallback_tests,
        ),
        Stage.FIX: IncidentAgent(
            name=AGENT_DISPLAY_NAMES[Stage.FIX],
            mention=agent_mention(Stage.FIX),
            stage=Stage.FIX,
            provider=Provider.FEATHERLESS,
            model_env="PATCH_GENERATOR_MODEL",
            default_model="Qwen/Qwen2.5-Coder-32B-Instruct",
            output_model=CandidatePatches,
            system_prompt=(
                "You are an expert software engineer generating patches for a production incident. "
                "You receive Pass 1 Docker logs, a regression test, AND 'repository_files' containing the actual source code. "
                "CRITICAL INSTRUCTION: You MUST base your patch on the actual code provided in 'repository_files'. "
                "Do NOT hallucinate file paths or code structures. "
                "Generate exactly two distinct candidate unified diffs in standard patch -p1 format. "
                "Return JSON only matching the CandidatePatches schema."
            ),
            fallback=_fallback_fix,
        ),
        Stage.RCA: IncidentAgent(
            name=AGENT_DISPLAY_NAMES[Stage.RCA],
            mention=agent_mention(Stage.RCA),
            stage=Stage.RCA,
            provider=Provider.OPENROUTER,
            model_env="RCA_MODEL",
            default_model="openai/gpt-oss-120b:free",
            output_model=RCAReport,
            system_prompt=(
                "You are an SRE writing a simple, formal, publishable root cause analysis report summary. "
                "You receive the winning patch, validation evidence, and the full incident context. "
                "Keep the final summary simple and easy to read for the end user. "
                "Your ONLY output is a valid JSON object matching the RCAReport schema — nothing else. "
                "Rules: "
                "1. title: a concise incident title (e.g. 'Checkout service crashes on null payload'). "
                "2. incident_summary: 2-3 sentences covering: what failed, when it was detected, who was impacted. "
                "3. customer_impact: one sentence on the end-user effect (e.g. '100% of checkout requests failed'). "
                "4. root_cause: MUST be the EXACT technical cause from the repro logs. "
                "   - If the error is a SyntaxError due to a missing colon, say: "
                "     'The function definition on line X was missing a colon (:), causing a SyntaxError.' "
                "   - If the error is a missing import, say: "
                "     'The module Y was not imported on line Z, causing a NameError.' "
                "   - ALWAYS include the specific error type, the exact syntax issue, and the line number if available. "
                "   - NEVER use vague phrases like 'syntax error in function' or 'code contained a syntax error.' "
                "   - Be precise: 'missing colon', 'missing parenthesis', 'undefined variable', etc. "
                "5. timeline: list at least 5 events in chronological order as plain-English strings "
                "   (e.g. '2024-01-15 14:32 UTC — Alert fired: checkout service returning 500'). "
                "   Include: alert fired, triage complete, repro confirmed, fix generated, validation passed, RCA published. "
                "6. contributing_factors: list 2-4 factors that allowed this bug to reach production. "
                "7. prevention_recommendations: list 3-5 actionable items to prevent recurrence. "
                "8. git_branch: branch name in format 'fix/<service>-<slug>' (e.g. 'fix/checkout-null-payload'). "
                "9. commit_message: conventional commit format: 'fix(<scope>): <what was fixed>' (max 72 chars). "
                "10. patch_unified_diff: copy the winning patch diff verbatim. "
                "11. validation_summary: one paragraph: which candidate won, what the test verified, exit code. "
                "12. final_markdown: a complete markdown RCA report (use ## headings for each section above). "
                "Be factual — only reference what appears in the repro_logs, patch, and context. Never invent details."
            ),
            fallback=_fallback_rca,
        ),
        Stage.VALIDATE: IncidentAgent(
    name=AGENT_DISPLAY_NAMES[Stage.VALIDATE],
    mention=agent_mention(Stage.VALIDATE),
    stage=Stage.VALIDATE,
    provider=Provider.FEATHERLESS,
    model_env="VALIDATION_MODEL",
    default_model="Qwen/Qwen2.5-Coder-32B-Instruct",
    output_model=ValidationReport,
    system_prompt=(
        "You are a senior QA Engineer responsible for validating candidate patches against a regression test suite.\n\n"
        "You receive:\n"
        "  - Two candidate unified diffs (CandidatePatches) from the Patch Generator\n"
        "  - The regression test written by the Test Architect\n"
        "  - Pass 1 Docker execution logs (pre-patch baseline)\n"
        "  - The full repository source files\n\n"
        "Your ONLY output is a valid JSON object matching the ValidationReport schema — nothing else.\n\n"
        "Rules:\n"
        "1. patch_results: for EACH candidate patch, produce a PatchResult entry containing:\n"
        "   a) patch_id: the identifier from CandidatePatches (e.g. 'patch_a', 'patch_b').\n"
        "   b) applies_cleanly: true if the diff applies without hunk failures, false otherwise.\n"
        "   c) test_exit_code: the exit code you predict the regression test will produce after the patch (0 = pass).\n"
        "   d) test_output_summary: 2-3 sentences describing what the test verifies and what the patched code does.\n"
        "   e) side_effects: list any unintended behaviour changes, new failure modes, or regressions the patch introduces.\n"
        "      If none, return an empty list.\n"
        "   f) correctness_score: integer 0-10 rating how completely this patch resolves the root cause.\n\n"
        "2. winning_patch_id: the patch_id of the candidate that achieves exit code 0, highest correctness_score,\n"
        "   and fewest side effects. If both patches fail, set to null.\n\n"
        "3. confidence: one of 'high', 'medium', or 'low'.\n"
        "   - high: winning patch passes all tests with no side effects.\n"
        "   - medium: winning patch passes but has minor side effects or residual uncertainty.\n"
        "   - low: both patches fail or the evidence is ambiguous.\n\n"
        "4. validation_notes: a single paragraph explaining your reasoning — which patch won, why the other lost,\n"
        "   and any caveats the RCA agent should be aware of.\n\n"
        "5. regression_risk: one of 'none', 'low', 'medium', or 'high' — your assessment of how likely the\n"
        "   winning patch is to introduce a regression in adjacent code paths.\n\n"
        "6. suggested_followup_tests: list 2-4 additional test cases (as plain English descriptions) that should\n"
        "   be written to improve coverage around the patched area.\n\n"
        "CONSTRAINTS:\n"
        "- Do NOT invent stack traces, log lines, or test output not present in the provided materials.\n"
        "- Do NOT apply patches speculatively — reason about them statically against the repo source.\n"
        "- If a patch modifies a file not present in repo_files, mark applies_cleanly as false and explain in side_effects.\n"
        "- If confidence is 'low', set winning_patch_id to null and explain fully in validation_notes."
    ),
    fallback=_fallback_validation,
),
    }




def _fallback_validation(error: Exception) -> ValidationReport:
    return ValidationReport(
        patch_results=[],
        winning_patch_id=None,
        confidence="low",
        validation_notes=f"Validation agent failed with error: {str(error)}. Manual review required.",
        regression_risk="high",
        suggested_followup_tests=[],
    )

def _alert_value(state: IncidentState, key: str, default: str) -> str:
    value = state.raw_alert.payload.get(key, default)
    return value if isinstance(value, str) else json.dumps(value)


def _service_label(state: IncidentState) -> str:
    alert = state.raw_alert.payload
    short = alert.get("service_short")
    if isinstance(short, str) and short.strip():
        return short.strip()
    return _alert_value(state, "service", "unknown-service")


def _fallback_triage(state: IncidentState) -> IncidentContext:
    service = _service_label(state)
    severity_raw = _alert_value(state, "severity", "sev2").lower()
    try:
        severity = Severity(severity_raw)
    except ValueError:
        severity = Severity.SEV2
    return IncidentContext(
        service=service,
        environment=_alert_value(state, "environment", "unknown"),
        error_signature=_alert_value(
            state,
            "error",
            _alert_value(state, "message", "unknown-error"),
        ),
        severity=severity,
        impact=_alert_value(state, "impact", "impact requires manual confirmation"),
        suspected_components=[service],
        evidence=[json.dumps(state.raw_alert.payload, separators=(",", ":"))],
        interpretations=["Fallback: deep analysis required by human"],
        investigation_plan=["Fallback: require human intervention"],
    )


def _fallback_repro(state: IncidentState) -> ReproPlan:
    service = state.context.service if state.context else "service"
    error = state.context.error_signature if state.context else "reported error"
    return ReproPlan(
        confirmed=False,
        assumptions=["Generated from alert payload because live inference was unavailable."],
        steps=[
            f"Run the configured repro_command in the local Docker image for {service}.",
            "Capture stdout and stderr from the failing state.",
            f"Confirm the observed signature: {error}.",
        ],
        expected_failure=error,
        required_data=["raw webhook payload", "service logs", "recent deploy diff"],
    )


def _demo_handler_rel_path(state: IncidentState) -> str | None:
    service = _service_label(state)
    slug = service.lower().replace("-", "_")
    candidates = [
        "services/checkout/handler.py",
        f"services/{service}/handler.py",
        f"services/{slug}/handler.py",
    ]
    seen: set[str] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if path in state.repo_files:
            return path
        if state.repo_path:
            if (Path(state.repo_path) / path).is_file():
                return path
    return None


def _placeholder_patch(index: int) -> CodePatch:
    return CodePatch(
        summary=f"Candidate {index + 1}: LLM required for repository-specific fix.",
        files_changed=[],
        patch_unified_diff=(
            f"--- a/placeholder_{index}.txt\n"
            f"+++ b/placeholder_{index}.txt\n"
            f"@@ -1 +1 @@\n"
            f"-before\n"
            f"+after{index}\n"
        ),
        risk_notes=["Generated because LIVE_LLM_ENABLED=false and no demo handler exists."],
        rollback_plan="No changes applied.",
    )


def _fallback_fix(state: IncidentState) -> CandidatePatches:
    # Repo-aware requirement: if we don't have repo context, never generate placeholder diffs.
    # This prevents committing hallucinated patches such as services/unknown-service/...
    raise RuntimeError(
        "FIX agent requires repository_files (state.repo_path must be set and loadable)."
    )



def _fallback_patch_diff(handler_path: str, index: int) -> str:
    _guard_messages = [
        "payload is required",
        "payload cannot be empty",
        "missing payload",
        "invalid payload",
        "payload validation failed",
    ]
    guard_message = _guard_messages[index % len(_guard_messages)]
    path = handler_path
    return (
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -8,1 +8,3 @@\n"
        "-    result = process(payload)\n"
        "+    if payload is None:\n"
        f"+        raise ValueError('{guard_message}')\n"
        "+    result = process(payload)\n"
    )


def _fallback_tests(state: IncidentState) -> RegressionTests:
    service = state.context.service if state.context else "service"
    module_path = service.replace("-", "_")
    return RegressionTests(
        framework="pytest",
        test_files=[f"tests/{service}/test_incident_regression.py"],
        test_code=(
            "import pytest\n\n"
            f"from services.{module_path}.handler import handle\n\n\n"
            "def test_rejects_missing_payload():\n"
            "    with pytest.raises(ValueError, match='payload is required'):\n"
            "        handle(None)\n"
        ),
        run_command="pytest tests/{service}/test_incident_regression.py".format(service=service),
        acceptance_criteria=["Regression test fails before the patch and passes after it."],
    )


def _fallback_rca(state: IncidentState) -> RCAReport:
    context = state.context or _fallback_triage(state)
    title = f"{context.service} incident RCA"
    winning_patch = state.fix
    validation_logs = _winning_validation_logs(state.validation)
    title_slug = _slug(context.service)
    final = (
        f"# {title}\n\n"
        f"## Summary\n{context.error_signature}\n\n"
        f"## Impact\n{context.impact}\n\n"
        "## Root Cause\nPending confirmation from repro and code review.\n\n"
        "## Remediation\nApply the winning patch, run regression tests, and monitor the service.\n"
    )
    return RCAReport(
        title=title,
        incident_summary=context.error_signature,
        customer_impact=context.impact,
        root_cause="Pending confirmation from repro and code review.",
        timeline=[handoff.created_at.isoformat() for handoff in state.band_thread],
        remediation=[winning_patch.summary if winning_patch else "Patch pending."],
        prevention=["Add alert-linked regression tests.", "Review deploy health gates."],
        git_branch=f"fix/{title_slug}-incident-{str(state.run_id)[:8]}",
        commit_message=f"Fix {context.service} incident regression",
        patch_unified_diff=winning_patch.patch_unified_diff if winning_patch else None,
        validation_summary=validation_logs,
        final_markdown=final,
    )


def _winning_validation_logs(validation: ValidationSwarmResult | None) -> str | None:
    if validation is None or validation.winning_candidate_index is None:
        return None
    for result in validation.results:
        if result.candidate_index == validation.winning_candidate_index:
            return result.logs
    return None


def _decode_exec_output(output: Any) -> str:
    if isinstance(output, tuple):
        return "\n".join(_decode_bytes(part) for part in output if part)
    return _decode_bytes(output)


def _decode_bytes(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _text_tar_archive(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        added_dirs: set[str] = set()
        for relative_path, content in files.items():
            safe_path = _safe_relative_container_path(relative_path)
            path = PurePosixPath(safe_path)
            parents = list(path.parents)[:-1]
            for parent in reversed(parents):
                if str(parent) == "." or str(parent) in added_dirs:
                    continue
                info = tarfile.TarInfo(str(parent))
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
                added_dirs.add(str(parent))

            encoded = content.encode("utf-8")
            info = tarfile.TarInfo(safe_path)
            info.size = len(encoded)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(encoded))
    return buffer.getvalue()


def _test_file_path(tests: RegressionTests, workdir: str) -> str:
    if tests.test_files:
        return _safe_relative_container_path(tests.test_files[0], workdir)
    return "tests/test_incident_regression.py"


def _safe_relative_container_path(path: str, workdir: str = "/workspace") -> str:
    normalized = path.replace("\\", "/")
    workdir = workdir.rstrip("/")
    if normalized.startswith(workdir + "/"):
        normalized = normalized[len(workdir) + 1 :]
    normalized = normalized.lstrip("/")
    parts = [part for part in PurePosixPath(normalized).parts if part not in ("", ".")]
    if not parts or any(part == ".." or ":" in part for part in parts):
        raise ValueError(f"unsafe container-relative path: {path}")
    return "/".join(parts)


def _alert_string(alert: dict[str, Any], keys: tuple[str, ...], default: str) -> str:
    for key in keys:
        value = alert.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return default


def _alert_optional_string(alert: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    value = _alert_string(alert, keys, "")
    return value or None


def _alert_int(alert: dict[str, Any], keys: tuple[str, ...], default: int) -> int:
    for key in keys:
        value = alert.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                continue
    return default


def _alert_bool(alert: dict[str, Any], keys: tuple[str, ...], default: bool) -> bool:
    for key in keys:
        value = alert.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in {"1", "true", "yes", "on"}
    return default


def _slug(value: str) -> str:
    slug = "".join(character.lower() if character.isalnum() else "-" for character in value)
    return "-".join(part for part in slug.split("-") if part) or "service"
