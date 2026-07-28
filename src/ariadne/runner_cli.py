"""Transient-unit entrypoint: resolve one recorded turn to a provider argv."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

from .adapters import ClaudeAdapter, CodexAdapter
from .adapters.claude import DEFAULT_ALLOWED_TOOLS, draft_allowed_tools
from .adapters.sessions import ProviderSessionController
from .clarification import (
    CLARIFICATION_FILENAME,
    InvalidClarification,
    parse_clarification_file,
)
from .config import Config
from .doctor import load_env_file
from .filesystem import StateLayout, ensure_private_file
from .hermes import HermesExecutionError, HermesExecutor
from .models import Provider, TurnKind
from .pipeline.gates import GateError, PipelineController
from .pipeline.task_parser import InvalidTask, parse_task
from .redaction import Redactor
from .retry_context import build_retry_context
from .runner import TurnRunner, build_child_environment
from .state import StateError, StateStore


def _default_env_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    ariadne = root / "ariadne" / "env"
    if ariadne.is_file():
        return ariadne
    return root / "dev-pipeline-harness" / "env"


def _record_clarification_outcome(
    *,
    state: StateStore,
    layout: StateLayout,
    pipeline_controller: PipelineController,
    session,
    pipeline,
    turn,
    redactor: Redactor,
) -> str:
    """Validate and persist a NEEDS_CLARIFICATION outcome; return the turn summary.

    The clarification protocol requires: no commit, no change besides the
    clarification file, and all five sections filled.  A violated protocol is
    reported as an ordinary gate failure so a half-implemented turn cannot
    disguise itself as a question.
    """

    clarification_path = session.worktree / CLARIFICATION_FILENAME
    head = pipeline_controller.worktree_head_sha(worktree=session.worktree)
    if head.lower() != (pipeline.task_start_head_sha or "").lower():
        return "clarification file present but commits were made; the TASK requires exactly one outcome"
    changes = pipeline_controller.changes_since_baseline(
        worktree=session.worktree,
        task_baseline_json=pipeline.task_baseline_json or "{}",
    )
    if set(changes) != {CLARIFICATION_FILENAME}:
        return "clarification must be the only worktree change besides the recorded baseline"
    try:
        request = parse_clarification_file(clarification_path)
    except InvalidClarification as exc:
        return f"clarification file is invalid: {exc}"
    copy_path = layout.session_dir(session.id) / f"clarification-{turn.id}.md"
    ensure_private_file(copy_path)
    copy_path.write_text(clarification_path.read_text(encoding="utf-8"), encoding="utf-8")
    copy_path.chmod(0o600)
    try:
        state.record_clarification(
            harness_session_id=session.id,
            turn_id=turn.id,
            blocker=redactor.redact(request.blocker),
            insufficiency=redactor.redact(request.insufficiency),
            options=redactor.redact(request.options),
            recommendation=redactor.redact(request.recommendation),
            impact=redactor.redact(request.impact),
        )
    except StateError:
        return "clarification could not be recorded; inspect the private session directory"
    state.record_audit(
        actor="hermes",
        action="hermes-clarification",
        harness_session_id=session.id,
        turn_id=turn.id,
        unit_name=turn.unit_name,
        details_json=json.dumps(
            {"raw_path": str(copy_path)}, ensure_ascii=False, sort_keys=True
        ),
    )
    # Remove the protocol file so the worktree returns to its recorded
    # baseline; the durable copy and DB row are the owner-facing artifacts.
    clarification_path.unlink(missing_ok=True)
    first_line = request.blocker.strip().splitlines()[0] if request.blocker.strip() else ""
    return "NEEDS_CLARIFICATION: " + redactor.redact(first_line)[:300]


def _run_hermes_turn(*, config: Config, state: StateStore, layout: StateLayout, turn, redactor: Redactor) -> dict:
    provider_session = state.get_provider_session(turn.provider_session_id)
    session = state.get_session(provider_session.harness_session_id)
    pipeline = state.get_pipeline_run(session.id)
    def fail(summary: str) -> dict:
        return TurnRunner(state=state, layout=layout, redactor=redactor).record_failure(turn.id, summary)

    pipeline_controller = PipelineController(
        state=state,
        redactor=redactor,
        profile=config.profile,
    )
    if pipeline.task_turn_id != turn.id or not pipeline.task_path or not pipeline.task_hash:
        return fail("recorded Hermes TASK is missing or does not belong to this turn")
    try:
        task_path = pipeline.task_path.resolve()
        task_path.relative_to(session.worktree.resolve())
        digest = hashlib.sha256(task_path.read_bytes()).hexdigest()
        if digest != pipeline.task_hash:
            raise RuntimeError("approved TASK changed after it was queued")
        task = pipeline_controller.scoped_task(parse_task(task_path))
    except (OSError, InvalidTask, ValueError):
        return fail("approved TASK is no longer usable")
    if not config.hermes_bin:
        return fail("HERMES_BIN is not configured")
    retry_context = build_retry_context(state, harness_session_id=session.id)
    knowledge_file = (
        config.profile.knowledge_file.as_posix() if config.profile.knowledge_file else None
    )
    try:
        command = HermesExecutor(executable=config.hermes_bin, redactor=redactor).build_command(
            task=task,
            worktree=session.worktree,
            branch=session.branch,
            retry_context=retry_context,
            knowledge_file=knowledge_file,
        )
    except HermesExecutionError:
        return fail("could not build the controlled Hermes command")
    state.record_audit(
        actor="runner",
        action="hermes-start",
        harness_session_id=session.id,
        turn_id=turn.id,
        unit_name=turn.unit_name,
        details_json=json.dumps(
            {
                "cwd": str(session.worktree),
                "task_path": str(task_path),
                "task_hash": pipeline.task_hash,
                "retry_context": bool(retry_context),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )

    def validate_completion() -> str | None:
        if (session.worktree / CLARIFICATION_FILENAME).exists():
            return _record_clarification_outcome(
                state=state,
                layout=layout,
                pipeline_controller=pipeline_controller,
                session=session,
                pipeline=pipeline,
                turn=turn,
                redactor=redactor,
            )
        try:
            pipeline_controller.verify_completed_task(
                session_id=session.id,
                task=task,
                task_baseline_json=pipeline.task_baseline_json or "",
                task_start_head_sha=pipeline.task_start_head_sha or "",
            )
        except GateError as exc:
            return str(exc)
        return None

    runner = TurnRunner(state=state, layout=layout, redactor=redactor)
    return runner.run(
        turn.id,
        command.argv,
        cwd=command.cwd,
        completion_validator=validate_completion,
    )


def run_recorded_turn(*, turn_id: str, env_path: Path) -> dict:
    values = load_env_file(env_path)
    config = Config.from_env(values)
    layout = StateLayout.from_state_dir(config.state_dir)
    redactor = Redactor(
        {secret for secret in (config.discord_token, config.github_token) if secret}
    )
    with StateStore(layout.db_path) as state:
        turn = state.get_turn(turn_id)
        provider_session = state.get_provider_session(turn.provider_session_id)
        harness_session = state.get_session(provider_session.harness_session_id)
        if turn.execution_kind is TurnKind.HERMES:
            return _run_hermes_turn(
                config=config,
                state=state,
                layout=layout,
                turn=turn,
                redactor=redactor,
            )
        pipeline_controller = PipelineController(
            state=state,
            redactor=redactor,
            profile=config.profile,
        )
        draft_start_head: str | None = None
        draft_start_baseline: dict[str, str] | None = None
        draft_initial_base_sha: str | None = None
        if turn.execution_kind is TurnKind.PROVIDER:
            draft_start_head = pipeline_controller.worktree_head_sha(
                worktree=harness_session.worktree
            )
            draft_start_baseline = pipeline_controller.snapshot_worktree_changes(
                worktree=harness_session.worktree
            )
            pipeline = state.get_pipeline_run(harness_session.id)
            if pipeline.task_turn_id is None:
                draft_initial_base_sha = pipeline.base_sha
        if not turn.input_path or not turn.input_path.is_file():
            raise RuntimeError("turn input pack is missing")
        prompt = turn.input_path.read_text(encoding="utf-8")
        if provider_session.provider is Provider.CODEX:
            if not config.codex_bin:
                raise RuntimeError("CODEX_BIN is not configured")
            adapter = CodexAdapter(
                config.codex_bin,
                allowed_models=config.codex_allowed_models,
                allowed_efforts=config.codex_allowed_efforts,
                redactor=redactor,
                workspace_network_access=config.codex_workspace_network_access,
            )
        else:
            if not config.claude_bin:
                raise RuntimeError("CLAUDE_BIN is not configured")
            # A provider turn is a drafting/consultation turn: read-only plus
            # write access scoped to the PLAN/TASK directories.  Review turns
            # override the tool list to a strict read-only set inside the
            # adapter, so passing draft tools here is harmless for them.
            claude_tools = (
                DEFAULT_ALLOWED_TOOLS
                if turn.execution_kind is TurnKind.REVIEW
                else draft_allowed_tools(
                    config.profile.plan_directory.as_posix(),
                    config.profile.task_directory.as_posix(),
                )
            )
            adapter = ClaudeAdapter(
                config.claude_bin,
                allowed_models=config.claude_allowed_models,
                allowed_efforts=config.claude_allowed_efforts,
                allowed_tools=claude_tools,
                redactor=redactor,
            )
        if turn.execution_kind is TurnKind.REVIEW:
            argv = adapter.review_command(
                model=turn.configured_model,
                prompt=prompt,
                effort=turn.configured_effort,
                base_ref=config.profile.base_ref,
            )
            command_type = "review"
        elif provider_session.provider_session_id:
            argv = adapter.resume_command(
                model=turn.configured_model,
                provider_session_id=provider_session.provider_session_id,
                prompt=prompt,
                effort=turn.configured_effort,
            )
            command_type = "resume"
        else:
            argv = adapter.new_command(
                model=turn.configured_model,
                prompt=prompt,
                effort=turn.configured_effort,
            )
            command_type = "new"
        try:
            version_result = subprocess.run(
                adapter.version_command(),
                cwd=harness_session.worktree,
                env=build_child_environment(),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("provider version check failed") from exc
        if version_result.returncode != 0 or not version_result.stdout.strip():
            raise RuntimeError("provider version check failed")
        cli_version = redactor.redact(version_result.stdout.strip())[:200]
        state.record_audit(
            actor="runner",
            action="provider-start",
            harness_session_id=harness_session.id,
            turn_id=turn.id,
            unit_name=turn.unit_name,
            details_json=json.dumps(
                {
                    "provider": provider_session.provider.value,
                    "cli_version": cli_version,
                    "cwd": str(harness_session.worktree),
                    "command_type": command_type,
                    "requested_model": turn.requested_model,
                    "configured_model": turn.configured_model,
                    "requested_effort": turn.requested_effort,
                    "configured_effort": turn.configured_effort,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        controller = ProviderSessionController(
            state=state,
            layout=layout,
            allowlists={
                Provider.CODEX: config.codex_allowed_models,
                Provider.CLAUDE: config.claude_allowed_models,
            },
            effort_allowlists={
                Provider.CODEX: config.codex_allowed_efforts,
                Provider.CLAUDE: config.claude_allowed_efforts,
            },
        )

        def handle_line(stream: str, line: str) -> None:
            # Dedicated review commands are intentionally not attached to the
            # implementation provider session.  Their session id is neither a
            # continuation target nor an authorization to edit the worktree.
            if turn.execution_kind is TurnKind.REVIEW:
                return
            if stream != "stdout":
                return
            for event in adapter.parse_line(line):
                controller.record_event(turn.id, event)

        runner = TurnRunner(state=state, layout=layout, redactor=redactor)

        def validate_provider_completion() -> str | None:
            if turn.execution_kind is not TurnKind.PROVIDER:
                return None
            assert draft_start_head is not None
            assert draft_start_baseline is not None
            try:
                if draft_initial_base_sha:
                    # Before the first Hermes handoff, validate the complete
                    # worktree against the recorded session base.  A retry may
                    # therefore repair an earlier failed drafting turn, while
                    # still being unable to carry code or commits across the
                    # approval boundary.
                    pipeline_controller.verify_initial_drafting_boundary(
                        worktree=harness_session.worktree,
                        base_sha=draft_initial_base_sha,
                    )
                else:
                    pipeline_controller.verify_drafting_turn(
                        worktree=harness_session.worktree,
                        start_head_sha=draft_start_head,
                        start_baseline=draft_start_baseline,
                    )
            except GateError as exc:
                return str(exc)
            return None

        return runner.run(
            turn.id,
            argv,
            cwd=harness_session.worktree,
            line_handler=handle_line,
            completion_validator=validate_provider_completion,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ariadne.runner_cli")
    parser.add_argument("--turn-id", required=True)
    parser.add_argument("--env-file", type=Path, default=_default_env_path())
    args = parser.parse_args(argv)
    result = run_recorded_turn(turn_id=args.turn_id, env_path=args.env_file)
    return 0 if result["state"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
