from __future__ import annotations

import asyncio
import io
import json
import os
import shlex
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Callable

from pydantic import BaseModel

from backend.inference import InferenceClients
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
)

Fallback = Callable[[IncidentState], BaseModel]

DEFAULT_CONTAINER_TIMEOUT_SECONDS = 60
MAX_VALIDATION_CANDIDATES = 2


def truncate_logs(log_string: str, max_lines: int = 200) -> str:
    """Return only the last max_lines from a Docker stdout/stderr string."""
    if max_lines <= 0:
        return ""
    return "\n".join(log_string.splitlines()[-max_lines:])


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
        prompt = json.dumps(state.model_dump(mode="json"), separators=(",", ":"))
        try:
            return await llm.json_call(
                provider=self.provider,
                model=self.model_name,
                system=self.system_prompt,
                user=prompt,
                output_model=self.output_model,
            )
        except Exception as exc:
            state.errors.append(f"{self.name}: {exc}")
            return self.fallback(state)

    @property
    def model_name(self) -> str:
        return os.getenv(self.model_env) or self.default_model


@dataclass(frozen=True)
class DockerSandboxConfig:
    image: str = "python:3.11-slim"
    repo_path: Path = field(default_factory=lambda: Path.cwd())
    workdir: str = "/workspace"
    source_mount: str = "/workspace_src"
    setup_command: str | None = None
    repro_command: str = "pytest"
    validation_command: str | None = None
    patch_strip: int = 1
    timeout_seconds: int = DEFAULT_CONTAINER_TIMEOUT_SECONDS
    network_disabled: bool = False

    @classmethod
    def from_alert(
        cls,
        alert: dict[str, Any],
        tests: RegressionTests | None = None,
    ) -> "DockerSandboxConfig":
        timeout = min(
            _alert_int(
                alert,
                ("container_timeout_seconds", "docker_timeout_seconds"),
                DEFAULT_CONTAINER_TIMEOUT_SECONDS,
            ),
            DEFAULT_CONTAINER_TIMEOUT_SECONDS,
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
                "python:3.11-slim",
            ),
            repo_path=repo_path.resolve(),
            workdir=_alert_string(alert, ("container_workdir", "workdir"), "/workspace"),
            source_mount=_alert_string(alert, ("source_mount",), "/workspace_src"),
            setup_command=_alert_optional_string(alert, ("setup_command", "docker_setup_command")),
            repro_command=_alert_string(
                alert,
                ("repro_command", "failing_command"),
                _alert_string(alert, ("test_command",), "pytest"),
            ),
            validation_command=validation_command,
            patch_strip=max(0, _alert_int(alert, ("patch_strip",), 1)),
            timeout_seconds=max(1, timeout),
            network_disabled=_alert_bool(
                alert,
                ("docker_network_disabled", "network_disabled"),
                False,
            ),
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
                f"cd {shlex.quote(self.config.workdir)} && "
                f"patch --batch --forward --fuzz=0 -p{self.config.patch_strip} "
                "-i /tmp/candidate.patch"
            )
            patch_code, _ = self._exec("patch", patch_command)
            if patch_code != 0:
                return self._validation_result(
                    candidate_index,
                    patch,
                    exit_code=patch_code,
                    error="strict patch command failed",
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
        source = self.config.source_mount.rstrip("/") + "/."
        destination = workdir.rstrip("/") + "/"
        command = (
            f"rm -rf {shlex.quote(workdir)} && "
            f"mkdir -p {shlex.quote(workdir)} && "
            f"cp -a {shlex.quote(source)} {shlex.quote(destination)}"
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
            failure_observed=exit_code is not None and exit_code != 0 and error is None,
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
            error=str(exc),
        )


async def run_validation_swarm(
    state: IncidentState,
    patches: CandidatePatches,
    tests: RegressionTests,
) -> ValidationSwarmResult:
    config = DockerSandboxConfig.from_alert(state.raw_alert.payload, tests)
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
        state = IncidentState(raw_alert=RawAlert(payload=alert))
        yield self._event(state, Stage.TRIAGE, "orchestrator", "queued", {"alert": alert})

        while state.current_stage not in {Stage.DONE, Stage.FAILED}:
            if state.steps_run >= state.max_steps:
                state.current_stage = Stage.FAILED
                yield self._event(
                    state,
                    Stage.FAILED,
                    "orchestrator",
                    "failed",
                    error="max_steps exceeded",
                )
                return

            if state.current_stage == Stage.VALIDATE:
                async for event in self._run_validation_stage(state):
                    yield event
                continue

            agent = self.agents[state.current_stage]
            yield self._event(state, agent.stage, agent.name, "active", self._stage_payload(state))

            output = await agent.run(state, self.llm)
            self._merge_output(state, agent.stage, output)

            if agent.stage == Stage.REPRO:
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
                        "to_agent": "Repro Sandbox",
                        "payload": output.model_dump(mode="json"),
                    },
                )
                yield self._event(
                    state,
                    Stage.REPRO,
                    "Repro Sandbox",
                    "active",
                    {"timeout_seconds": DEFAULT_CONTAINER_TIMEOUT_SECONDS},
                )
                state.repro_execution = await run_repro_pass1(
                    state,
                    output,  # type: ignore[arg-type]
                )
                yield self._event(
                    state,
                    Stage.REPRO,
                    "Repro Sandbox",
                    "complete",
                    state.repro_execution.model_dump(mode="json"),
                )

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
                        "to_agent": next_agent_name,
                        "payload": self._stage_payload(state),
                    },
                )

            state.current_stage = next_stage

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
            },
        )

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
            yield self._event(
                state,
                Stage.FAILED,
                "Validation Swarm",
                "failed",
                error="validation requires candidate patches and regression tests",
            )
            return

        state.validation = await run_validation_swarm(state, state.candidate_patches, state.tests)
        state.fix = state.validation.winning_patch
        state.steps_run += 1

        if state.fix is None:
            state.current_stage = Stage.FAILED
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
                "to_agent": next_agent_name,
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
            name="Alert Triager",
            mention="@incident-triager",
            stage=Stage.TRIAGE,
            provider=Provider.AIML,
            model_env="TRIAGE_MODEL",
            default_model="gpt-4o-mini",
            output_model=IncidentContext,
            system_prompt=(
                "Extract incident JSON from webhook data. Use a fast, cheap model path. "
                "Return JSON only."
            ),
            fallback=_fallback_triage,
        ),
        Stage.REPRO: IncidentAgent(
            name="Repro Planner",
            mention="@incident-reproducer",
            stage=Stage.REPRO,
            provider=Provider.AIML,
            model_env="REPRO_MODEL",
            default_model="gpt-4o",
            output_model=ReproPlan,
            system_prompt=(
                "Plan Pass 1 reproduction for a local Docker sandbox. Return deterministic failing "
                "state expectations as JSON only."
            ),
            fallback=_fallback_repro,
        ),
        Stage.TEST: IncidentAgent(
            name="Regression Test Generator",
            mention="@incident-test-generator",
            stage=Stage.TEST,
            provider=Provider.AIML,
            model_env="REGRESSION_TEST_MODEL",
            default_model="gpt-4o",
            output_model=RegressionTests,
            system_prompt=(
                "Read Pass 1 Docker logs and write exactly one strict failing pytest "
                "regression test. "
                "Return JSON only."
            ),
            fallback=_fallback_tests,
        ),
        Stage.FIX: IncidentAgent(
            name="Patch Generator",
            mention="@incident-patch-generator",
            stage=Stage.FIX,
            provider=Provider.FEATHERLESS,
            model_env="PATCH_GENERATOR_MODEL",
            default_model="Qwen/Qwen2.5-Coder-32B-Instruct",
            output_model=CandidatePatches,
            system_prompt=(
                "Use the Pass 1 logs plus the new regression test to produce exactly two distinct "
                "candidate unified diffs. Return JSON only."
            ),
            fallback=_fallback_fix,
        ),
        Stage.RCA: IncidentAgent(
            name="RCA Publisher",
            mention="@incident-rca-writer",
            stage=Stage.RCA,
            provider=Provider.AIML,
            model_env="RCA_MODEL",
            default_model="gpt-4o",
            output_model=RCAReport,
            system_prompt=(
                "Use the winning patch and passing validation logs to produce final RCA/Git JSON. "
                "Return JSON only."
            ),
            fallback=_fallback_rca,
        ),
    }


def _alert_value(state: IncidentState, key: str, default: str) -> str:
    value = state.raw_alert.payload.get(key, default)
    return value if isinstance(value, str) else json.dumps(value)


def _fallback_triage(state: IncidentState) -> IncidentContext:
    return IncidentContext(
        service=_alert_value(state, "service", "unknown-service"),
        environment=_alert_value(state, "environment", "unknown"),
        error_signature=_alert_value(
            state,
            "error",
            _alert_value(state, "message", "unknown-error"),
        ),
        severity=Severity.SEV2,
        impact=_alert_value(state, "impact", "impact requires manual confirmation"),
        suspected_components=[_alert_value(state, "service", "unknown-service")],
        evidence=[json.dumps(state.raw_alert.payload, separators=(",", ":"))],
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


def _fallback_fix(state: IncidentState) -> CandidatePatches:
    service = state.context.service if state.context else "service"
    candidates = [
        CodePatch(
            summary=(
                f"Candidate {index + 1}: add defensive validation around failing path "
                f"in {service}."
            ),
            files_changed=[f"services/{service}/handler.py"],
            patch_unified_diff=_fallback_patch_diff(service, index),
            risk_notes=["Placeholder patch; replace with repository-specific diff after repro."],
            rollback_plan="Revert the patch commit and redeploy the previous stable artifact.",
        )
        for index in range(MAX_VALIDATION_CANDIDATES)
    ]
    return CandidatePatches(candidates=candidates)


def _fallback_patch_diff(service: str, index: int) -> str:
    guard_message = [
        "payload is required",
        "payload cannot be empty",
        "missing payload",
        "invalid payload",
        "payload validation failed",
    ][index]
    return (
        f"--- a/services/{service}/handler.py\n"
        f"+++ b/services/{service}/handler.py\n"
        "@@\n"
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
