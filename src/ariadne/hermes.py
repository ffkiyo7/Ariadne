"""Controlled local Hermes executor.

The Hermes HTTP API is intentionally not an implementation path for code
tasks because it cannot carry a trustworthy cwd/worktree field.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .pipeline.task_parser import TaskSpec
from .redaction import Redactor


class HermesExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class HermesCommand:
    argv: tuple[str, ...]
    cwd: Path


class HermesExecutor:
    def __init__(self, *, executable: Path, redactor: Redactor | None = None):
        self.executable = Path(executable)
        self.redactor = redactor or Redactor()
        if not self.executable.is_absolute():
            raise HermesExecutionError("Hermes executable must be an absolute path")

    def build_command(self, *, task: TaskSpec, worktree: Path, branch: str) -> HermesCommand:
        if not worktree.is_absolute():
            raise HermesExecutionError("Hermes worktree must be absolute")
        prompt = (
            "Execute exactly this approved TASK in the current working directory.\n\n"
            + task.prompt_constraints(worktree=worktree, branch=branch)
            + "\n\nBefore exiting, run the listed verification commands and create one local commit "
            "containing only the allowed changes. Do not push, merge, reset, clean, or widen the TASK."
        )
        return HermesCommand(
            argv=(str(self.executable), "-z", prompt),
            cwd=worktree,
        )

    def run(self, command: HermesCommand, *, env: dict[str, str] | None = None) -> int:
        del command, env
        raise HermesExecutionError(
            "Hermes implementation turns must be launched by Ariadne's transient-unit runner"
        )


class HermesHTTPExecutor:
    def run(self, *_args, **_kwargs):
        raise HermesExecutionError("HTTP Hermes runs are forbidden for code tasks because cwd is not enforced")
