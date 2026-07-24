"""Controlled local Hermes executor.

The Hermes HTTP API is intentionally not an implementation path for code
tasks because it cannot carry a trustworthy cwd/worktree field.

The prompt built here encodes the collaboration contract: two mutually
exclusive terminal outcomes (implement, or ask), objective conditions that
*require* asking, and a commit-message self-report that the owner reads
instead of the diff.  The clarification triggers are deliberately phrased as
rules rather than judgment calls: a compliance-biased model is better at
following rules than at volunteering doubt, so the design leans on the bias
instead of fighting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .clarification import CLARIFICATION_FILENAME
from .pipeline.task_parser import TaskSpec
from .redaction import Redactor


class HermesExecutionError(RuntimeError):
    pass


_RETRY_CONTEXT_LIMIT = 3000


@dataclass(frozen=True)
class HermesCommand:
    argv: tuple[str, ...]
    cwd: Path


def _clarification_protocol() -> str:
    return (
        "## Two terminal outcomes - choose exactly one\n\n"
        "Before changing any file, read the whole TASK and decide:\n\n"
        "(A) IMPLEMENT - the TASK is unambiguous and achievable within its allowed files. "
        "Implement it, run every listed verification command, and create exactly one local commit "
        "containing only the allowed changes.\n\n"
        f"(B) CLARIFY - create the single file `{CLARIFICATION_FILENAME}` at the worktree root, "
        "make no other change and no commit, then stop.\n\n"
        "Producing both, or neither, is a failure.\n\n"
        "You MUST choose CLARIFY when any of the following holds. These are rules, not suggestions:\n"
        "- The TASK references a file, function, or interface that does not exist in this worktree.\n"
        "- A change you consider necessary falls outside the allowed-files list.\n"
        "- Two TASK sections contradict each other.\n"
        "- A verification command already fails on the untouched worktree.\n"
        "- The definition of done cannot be checked by the listed verification commands.\n"
        "- Two or more materially different implementations satisfy the TASK text, and the choice "
        "changes behavior, data, or an interface.\n\n"
        "A justified CLARIFY is a successful outcome. A plausible-looking implementation of the "
        "wrong interpretation is a failure. Do not guess in order to appear agreeable.\n\n"
        f"`{CLARIFICATION_FILENAME}` must contain exactly these Markdown sections, each with real "
        "content:\n\n"
        "## Blocker\n"
        "## Why the TASK is insufficient\n"
        "## Options\n"
        "## Recommendation\n"
        "## Why / Impact\n\n"
        "State your own recommendation and reasons. Asking without a recommendation is not an "
        "acceptable clarification."
    )


def _self_report_contract() -> str:
    return (
        "## Commit self-report (outcome A only)\n\n"
        "The owner reads your commit message instead of the diff. The single commit message must "
        "contain, in this order:\n"
        "1. A short subject line.\n"
        "2. A body in your own words: what you did, why you did it this way, and your own judgment "
        "of the risks.\n"
        "3. A literal final section listing every file you changed:\n\n"
        "Files:\n"
        "- path/to/first-file\n"
        "- path/to/second-file\n\n"
        "The file list is cross-checked against Git. A missing or wrong entry is treated as a defect."
    )


def _knowledge_section(knowledge_file: str) -> str:
    return (
        "## Project knowledge\n\n"
        f"Read `AGENTS.md` and `{knowledge_file}` (when present) before implementing; they are "
        "project memory left by previous work.\n"
        f"If you discover durable, non-obvious project knowledge while implementing - a pitfall, an "
        f"invariant, a command that matters - append a short dated entry to `{knowledge_file}` in "
        "the same commit. Keep entries factual and at most 10 lines. Never rewrite or delete "
        "existing entries. Do not record speculation or restate the TASK."
    )


def _retry_section(retry_context: str) -> str:
    bounded = retry_context.strip()[:_RETRY_CONTEXT_LIMIT]
    return (
        "## Previous round evidence\n\n"
        "An earlier attempt of this TASK did not reach review. Address every item below "
        "explicitly before implementing; repeating the same failure is the worst outcome.\n\n"
        + bounded
    )


class HermesExecutor:
    def __init__(self, *, executable: Path, redactor: Redactor | None = None):
        self.executable = Path(executable)
        self.redactor = redactor or Redactor()
        if not self.executable.is_absolute():
            raise HermesExecutionError("Hermes executable must be an absolute path")

    def build_command(
        self,
        *,
        task: TaskSpec,
        worktree: Path,
        branch: str,
        retry_context: str | None = None,
        knowledge_file: str | None = None,
    ) -> HermesCommand:
        if not worktree.is_absolute():
            raise HermesExecutionError("Hermes worktree must be absolute")
        sections = [
            "Execute exactly this approved TASK in the current working directory.",
            task.prompt_constraints(worktree=worktree, branch=branch),
            _clarification_protocol(),
            _self_report_contract(),
        ]
        if knowledge_file:
            sections.append(_knowledge_section(knowledge_file))
        if retry_context:
            sections.append(_retry_section(self.redactor.redact(retry_context)))
        sections.append(
            "Do not push, merge, reset, clean, expose secrets, or widen the TASK scope."
        )
        prompt = "\n\n".join(sections)
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
