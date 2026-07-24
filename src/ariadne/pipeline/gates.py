"""Deterministic PLAN/TASK/review/PR/CI/preview/accept gates."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..github import ChecksFacts, GitHubClient, GitHubError, PullRequestFacts
from ..models import SessionStatus
from ..preview import PreviewHealthChecker
from ..redaction import Redactor
from ..runner import build_child_environment
from ..state import InvalidTransition, NotFoundError, StateError, StateStore
from ..profile import ProjectProfile
from .task_parser import InvalidTask, TaskSpec, parse_task


class GateError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerificationResult:
    command: tuple[str, ...]
    passed: bool
    summary: str


@dataclass(frozen=True)
class AcceptFacts:
    pr: PullRequestFacts
    checks: ChecksFacts
    preview_healthy: bool


class PipelineController:
    def __init__(
        self,
        *,
        state: StateStore,
        preview_checker: PreviewHealthChecker | None = None,
        command_runner=subprocess.run,
        redactor: Redactor | None = None,
        profile: ProjectProfile | None = None,
    ):
        self.state = state
        self.preview_checker = preview_checker or PreviewHealthChecker()
        self.command_runner = command_runner
        self.redactor = redactor or Redactor()
        self.profile = profile or ProjectProfile.default()

    def register_plan(self, *, session_id: str, plan_path: Path, base_sha: str) -> None:
        session = self.state.get_session(session_id)
        if not re.fullmatch(r"[0-9a-fA-F]{40}", base_sha):
            raise GateError("PLAN base SHA must be a full 40-character commit SHA")
        plan_path = Path(plan_path).resolve()
        try:
            plan_path.relative_to(session.worktree.resolve())
        except ValueError as exc:
            raise GateError("PLAN must be inside the session worktree") from exc
        if plan_path.suffix != ".md" or not plan_path.exists():
            raise GateError("PLAN must be an existing Markdown file")
        plan_hash = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        try:
            self.state.create_pipeline_run(
                session_id,
                plan_path=plan_path,
                plan_hash=plan_hash,
                base_sha=base_sha,
            )
        except StateError as exc:
            try:
                self.state.get_pipeline_run(session_id)
            except NotFoundError:
                raise GateError("pipeline record is missing") from exc
            self.state.update_pipeline_run(
                session_id,
                plan_path=plan_path,
                plan_hash=plan_hash,
                base_sha=base_sha,
            )

    def scoped_task(self, task: TaskSpec) -> TaskSpec:
        """Extend a TASK allowlist with the profile's shared knowledge file.

        The knowledge file is team memory: a worker may append findings in
        the same reviewed commit without the TASK author having to remember
        to allow it.  Every other scope rule stays exactly as authored.
        """

        knowledge = self.profile.knowledge_file
        if knowledge is None:
            return task
        value = knowledge.as_posix()
        if task.allows_path(value):
            return task
        return dataclasses.replace(task, allowed_files=task.allowed_files + (value,))

    def validate_task(self, *, session_id: str, task_path: Path) -> TaskSpec:
        session = self.state.get_session(session_id)
        task_path = Path(task_path).resolve()
        try:
            task_path.relative_to(session.worktree.resolve())
        except ValueError as exc:
            raise GateError("TASK must be inside the session worktree") from exc
        try:
            task = parse_task(task_path)
            task.validate_hermes_contract()
            return self.scoped_task(task)
        except InvalidTask as exc:
            raise GateError(str(exc)) from exc

    def changed_paths(self, *, worktree: Path, start_head_sha: str | None = None) -> tuple[str, ...]:
        paths: set[str] = set()
        if start_head_sha is not None and not re.fullmatch(r"[0-9a-fA-F]{40}", start_head_sha):
            raise GateError("TASK start head SHA must be a full 40-character commit SHA")
        try:
            commands = [
                ["git", "-C", str(worktree), "diff", "--name-only", "HEAD"],
                ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard"],
            ]
            if start_head_sha is not None:
                commands.insert(
                    0,
                    ["git", "-C", str(worktree), "diff", "--name-only", f"{start_head_sha}..HEAD"],
                )
            for command in commands:
                result = self.command_runner(
                    command,
                    cwd=worktree,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=build_child_environment(),
                )
                paths.update(path for path in result.stdout.splitlines() if path.strip())
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GateError("could not inspect worktree diff") from exc
        return tuple(sorted(paths))

    def worktree_head_sha(self, *, worktree: Path) -> str:
        try:
            result = self.command_runner(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                cwd=worktree,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=build_child_environment(),
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GateError("could not resolve the TASK start head SHA") from exc
        head_sha = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
            raise GateError("could not resolve the TASK start head SHA")
        return head_sha

    @staticmethod
    def _fingerprint_path(*, worktree: Path, relative_path: str) -> str:
        normalized = relative_path.replace("\\", "/")
        parts = Path(normalized).parts
        if normalized.startswith("/") or ".." in parts or not normalized:
            raise GateError("Git reported an unsafe changed path")
        root = Path(worktree).resolve()
        path = root / normalized
        try:
            path.relative_to(root)
        except ValueError as exc:  # pragma: no cover - protected above
            raise GateError("changed path falls outside the worktree") from exc
        try:
            stat_result = path.lstat()
        except FileNotFoundError:
            return "missing"
        except OSError as exc:
            raise GateError("could not fingerprint changed path") from exc
        if path.is_symlink():
            try:
                target = os.readlink(path)
            except OSError as exc:
                raise GateError("could not fingerprint changed symlink") from exc
            return "symlink:" + hashlib.sha256(target.encode("utf-8", errors="surrogateescape")).hexdigest()
        if not path.is_file():
            return f"unsupported:{stat_result.st_mode:o}"
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError as exc:
            raise GateError("could not fingerprint changed file") from exc
        return "file:" + digest.hexdigest()

    def snapshot_task_baseline(self, *, worktree: Path) -> dict[str, str]:
        """Capture already-dirty PLAN/TASK files before a weak-model turn.

        Strong-model PLAN/TASK drafting may intentionally leave Markdown files
        uncommitted.  Scope validation must therefore compare the final
        worktree with this snapshot, rather than treating those pre-existing
        files as Hermes edits.
        """

        return {
            path: self._fingerprint_path(worktree=worktree, relative_path=path)
            for path in self.changed_paths(worktree=worktree)
        }

    @staticmethod
    def _parse_task_baseline(task_baseline_json: str) -> dict[str, str]:
        try:
            raw_baseline = json.loads(task_baseline_json)
        except (TypeError, ValueError) as exc:
            raise GateError("TASK baseline is missing or malformed") from exc
        if (
            not isinstance(raw_baseline, dict)
            or any(not isinstance(path, str) or not isinstance(value, str) for path, value in raw_baseline.items())
        ):
            raise GateError("TASK baseline is malformed")
        return dict(raw_baseline)

    def _changes_since_task_baseline(
        self,
        *,
        worktree: Path,
        baseline: dict[str, str],
    ) -> tuple[str, ...]:
        """Return dirty/untracked paths that are not the frozen PLAN/TASK baseline."""

        dirty_paths = self.changed_paths(worktree=worktree)
        current = {
            path: self._fingerprint_path(worktree=worktree, relative_path=path)
            for path in set(baseline) | set(dirty_paths)
        }
        return tuple(
            sorted(
                path
                for path in set(baseline) | set(current)
                if baseline.get(path) != current.get(path)
            )
        )

    def changes_since_baseline(
        self,
        *,
        worktree: Path,
        task_baseline_json: str,
    ) -> tuple[str, ...]:
        """Public view of baseline drift, used by the clarification gate."""

        baseline = self._parse_task_baseline(task_baseline_json)
        return self._changes_since_task_baseline(worktree=worktree, baseline=baseline)

    def verify_reviewed_worktree_clean(self, *, session_id: str) -> None:
        """Permit only unchanged pre-Hermes PLAN/TASK files before a Draft PR.

        PLAN and TASK documents are deliberately kept as local owner/audit
        artifacts.  They may be untracked on a reviewed branch, but must be
        byte-for-byte identical to the snapshot taken before Hermes started.
        Any other staged, unstaged, untracked, or deleted path blocks a push.
        """

        session = self.state.get_session(session_id)
        pipeline = self.state.get_pipeline_run(session_id)
        baseline = self._parse_task_baseline(pipeline.task_baseline_json or "{}")
        dirty_changes = self._changes_since_task_baseline(
            worktree=session.worktree,
            baseline=baseline,
        )
        if dirty_changes:
            raise GateError("worktree has changes outside the recorded TASK baseline")

    def verify_task_scope(self, *, task: TaskSpec, changed_paths: Sequence[str]) -> None:
        try:
            task.validate_changed_paths(list(changed_paths))
        except InvalidTask as exc:
            raise GateError(str(exc)) from exc

    def verify_completed_task(
        self,
        *,
        session_id: str,
        task: TaskSpec,
        task_baseline_json: str,
        task_start_head_sha: str,
    ) -> tuple[VerificationResult, ...]:
        """Run the deterministic post-Hermes gate before review is unlocked."""

        session = self.state.get_session(session_id)
        baseline = self._parse_task_baseline(task_baseline_json)
        current_head_sha = self.worktree_head_sha(worktree=session.worktree)
        try:
            ancestor = self.command_runner(
                [
                    "git",
                    "-C",
                    str(session.worktree),
                    "merge-base",
                    "--is-ancestor",
                    task_start_head_sha,
                    current_head_sha,
                ],
                cwd=session.worktree,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=build_child_environment(),
            )
            count_result = self.command_runner(
                [
                    "git",
                    "-C",
                    str(session.worktree),
                    "rev-list",
                    "--count",
                    f"{task_start_head_sha}..{current_head_sha}",
                ],
                cwd=session.worktree,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=build_child_environment(),
            )
            committed_result = self.command_runner(
                [
                    "git",
                    "-C",
                    str(session.worktree),
                    "diff",
                    "--name-only",
                    f"{task_start_head_sha}..{current_head_sha}",
                ],
                cwd=session.worktree,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=build_child_environment(),
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GateError("could not verify Hermes commit state") from exc
        if ancestor.returncode != 0:
            raise GateError("Hermes branch no longer descends from the approved TASK HEAD")
        try:
            commit_count = int(count_result.stdout.strip())
        except ValueError as exc:
            raise GateError("could not count Hermes commits") from exc
        if commit_count != 1:
            raise GateError("Hermes must create exactly one local commit for the approved TASK")
        committed_paths = tuple(sorted(path for path in committed_result.stdout.splitlines() if path.strip()))
        self.verify_task_scope(task=task, changed_paths=committed_paths)

        dirty_changes = self._changes_since_task_baseline(
            worktree=session.worktree,
            baseline=baseline,
        )
        self.verify_task_scope(task=task, changed_paths=dirty_changes)
        changed = tuple(sorted(set(committed_paths) | set(dirty_changes)))
        results = self.run_verification(
            worktree=session.worktree,
            commands=task.verification_commands,
        )
        failed = [result for result in results if not result.passed]
        details = {
            "task_path": str(task.path),
            "head_sha": current_head_sha,
            "commit_count": commit_count,
            "committed_paths": committed_paths,
            "dirty_paths": dirty_changes,
            "changed_paths": changed,
            "verification": [
                {
                    "command": list(result.command),
                    "passed": result.passed,
                    # Failed output is retry-loop evidence; passed output is noise.
                    **({} if result.passed else {"summary": result.summary[:600]}),
                }
                for result in results
            ],
        }
        self.state.record_audit(
            actor="ariadne",
            action="task-verified" if not failed else "task-verification-failed",
            harness_session_id=session_id,
            details_json=json.dumps(details, ensure_ascii=False, sort_keys=True),
        )
        if failed:
            raise GateError("TASK verification failed")
        return results

    def run_verification(
        self,
        *,
        worktree: Path,
        commands: Sequence[Sequence[str] | str],
    ) -> tuple[VerificationResult, ...]:
        results: list[VerificationResult] = []
        for command in commands:
            if isinstance(command, str):
                try:
                    argv = tuple(shlex.split(command))
                except ValueError as exc:
                    raise GateError("verification command has invalid quoting") from exc
            else:
                argv = tuple(command)
            if not argv or any(not isinstance(arg, str) or not arg for arg in argv):
                raise GateError("verification command must be an argv list")
            if any(token in {";", "&&", "||", "|", ">", ">>", "<"} or "$(" in token or "`" in token for token in argv):
                raise GateError("verification commands may not contain shell operators")
            try:
                completed = self.command_runner(
                    list(argv),
                    cwd=worktree,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=build_child_environment(),
                )
            except OSError as exc:
                results.append(VerificationResult(argv, False, "command could not be started"))
                continue
            summary = self.redactor.redact((completed.stdout + "\n" + completed.stderr).strip())
            summary = summary[-1000:] if summary else ""
            results.append(VerificationResult(argv, completed.returncode == 0, summary))
        return tuple(results)

    def record_review(
        self,
        *,
        session_id: str,
        passed: bool,
        summary: str,
        actor: str = "strong-model",
    ) -> SessionStatus:
        pipeline = self.state.get_pipeline_run(session_id)
        session = self.state.get_session(session_id)
        if passed and session.status is not SessionStatus.REVIEW_PENDING:
            raise GateError("review can pass only from review_pending")
        if not passed and session.status not in {SessionStatus.REVIEW_PENDING, SessionStatus.TASK_RUNNING}:
            raise GateError("review failure can only return to task/review state")
        round_number = pipeline.review_round + 1
        self.state.update_pipeline_run(session_id, review_round=round_number)
        if passed:
            self.state.record_audit(
                actor=actor,
                action="review-passed",
                harness_session_id=session_id,
                details_json=json.dumps(
                    {"round": round_number, "summary": self.redactor.redact(summary)[:2000]},
                    ensure_ascii=False,
                ),
            )
            return session.status
        self.state.record_audit(
            actor=actor,
            action="review-failed",
            harness_session_id=session_id,
            details_json=json.dumps(
                {"round": round_number, "summary": self.redactor.redact(summary)[:2000]},
                ensure_ascii=False,
            ),
        )
        if round_number >= 2:
            if session.status is not SessionStatus.NEEDS_OWNER:
                try:
                    self.state.transition_session(session_id, SessionStatus.NEEDS_OWNER)
                except InvalidTransition as exc:
                    raise GateError("failed review requires owner intervention") from exc
            return SessionStatus.NEEDS_OWNER
        if session.status is SessionStatus.REVIEW_PENDING:
            self.state.transition_session(session_id, SessionStatus.TASK_RUNNING)
        return SessionStatus.TASK_RUNNING

    def open_draft_pr(
        self,
        *,
        session_id: str,
        github: GitHubClient,
        title: str,
        body: str,
        head_sha: str,
    ) -> PullRequestFacts:
        session = self.state.get_session(session_id)
        pipeline = self.state.get_pipeline_run(session_id)
        if not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
            raise GateError("PR head SHA must be a full 40-character commit SHA")
        if session.status is not SessionStatus.REVIEW_PENDING:
            raise GateError("PR can open only after review_pending")
        if not self.profile.owns_branch(session.branch):
            raise GateError("PR branch is outside the pipeline namespace")
        if not pipeline.plan_path or not pipeline.plan_path.is_file():
            raise GateError("recorded PLAN is missing")
        if pipeline.plan_hash:
            current_plan_hash = hashlib.sha256(pipeline.plan_path.read_bytes()).hexdigest()
            if current_plan_hash != pipeline.plan_hash:
                raise GateError("recorded PLAN changed after approval")
        try:
            branch_result = self.command_runner(
                ["git", "-C", str(session.worktree), "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=session.worktree,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=build_child_environment(),
            )
            head_result = self.command_runner(
                ["git", "-C", str(session.worktree), "rev-parse", "HEAD"],
                cwd=session.worktree,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=build_child_environment(),
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GateError("could not verify worktree before opening PR") from exc
        if branch_result.stdout.strip() != session.branch:
            raise GateError("worktree branch does not match the Harness session")
        if head_result.stdout.strip().lower() != head_sha.lower():
            raise GateError("worktree HEAD does not match the proposed PR head SHA")
        self.verify_reviewed_worktree_clean(session_id=session_id)
        try:
            facts = github.create_draft_pr(
                branch=session.branch,
                title=self.redactor.redact(title)[:300],
                body=self.redactor.redact(body),
            )
        except GitHubError as exc:
            raise GateError("could not create draft PR") from exc
        if not facts.is_draft or facts.state.upper() != "OPEN" or facts.head_branch != session.branch or facts.head_sha.lower() != head_sha.lower():
            raise GateError("new PR facts did not match the recorded head")
        self.state.update_pipeline_run(session_id, pr_number=facts.number, head_sha=head_sha)
        self.state.transition_session(session_id, SessionStatus.PR_OPEN)
        self.state.record_audit(
            actor="harness",
            action="draft-pr-opened",
            harness_session_id=session_id,
            details_json=json.dumps(
                {"pr_number": facts.number, "head_sha": facts.head_sha},
                ensure_ascii=False,
            ),
        )
        return facts

    def push_branch(self, *, session_id: str) -> str:
        """Push one reviewed session branch, never a base branch or arbitrary ref."""

        session = self.state.get_session(session_id)
        if session.status is not SessionStatus.REVIEW_PENDING:
            raise GateError("branch can be pushed only after review_pending")
        if not self.profile.owns_branch(session.branch):
            raise GateError("branch is outside the configured Ariadne namespace")
        try:
            branch = self.command_runner(
                ["git", "-C", str(session.worktree), "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=session.worktree,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=build_child_environment(),
            ).stdout.strip()
            head = self.command_runner(
                ["git", "-C", str(session.worktree), "rev-parse", "HEAD"],
                cwd=session.worktree,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=build_child_environment(),
            ).stdout.strip()
            if branch != session.branch or not re.fullmatch(r"[0-9a-fA-F]{40}", head):
                raise GateError("worktree is not a reviewed session branch")
            self.verify_reviewed_worktree_clean(session_id=session_id)
            self.command_runner(
                ["git", "-C", str(session.worktree), "push", "--set-upstream", self.profile.git_remote, session.branch],
                cwd=session.worktree,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=build_child_environment(),
            )
        except GateError:
            raise
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GateError("could not push reviewed session branch") from exc
        self.state.record_audit(
            actor="ariadne",
            action="reviewed-branch-pushed",
            harness_session_id=session_id,
            details_json=json.dumps({"branch": session.branch, "head_sha": head}, ensure_ascii=False),
        )
        return head

    def record_ci(self, *, session_id: str, github: GitHubClient) -> ChecksFacts:
        pipeline = self.state.get_pipeline_run(session_id)
        if pipeline.pr_number is None:
            raise GateError("CI cannot be recorded without a PR")
        facts = github.get_checks(pipeline.pr_number)
        if not facts.green:
            raise GateError("CI is not green")
        self.state.update_pipeline_run(session_id, ci_state="passed")
        self.state.transition_session(session_id, SessionStatus.CI_PASSED)
        self.state.record_audit(
            actor="harness",
            action="ci-passed",
            harness_session_id=session_id,
            details_json=json.dumps({"summaries": facts.summaries}, ensure_ascii=False),
        )
        return facts

    def record_preview(self, *, session_id: str, url: str) -> None:
        pipeline = self.state.get_pipeline_run(session_id)
        if not pipeline.pr_number:
            raise GateError("preview cannot be recorded without a PR")
        facts = self.preview_checker.check(url)
        if not facts.healthy:
            raise GateError("preview health gate failed")
        self.state.update_pipeline_run(session_id, preview_url=url)
        self.state.transition_session(session_id, SessionStatus.PREVIEW_READY)
        self.state.record_audit(
            actor="harness",
            action="preview-ready",
            harness_session_id=session_id,
            details_json=json.dumps({"preview_url": self.redactor.redact(url)}, ensure_ascii=False),
        )

    def accept(
        self,
        *,
        session_id: str,
        caller_id: str,
        owner_id: str,
        pr_number: int,
        full_head_sha: str,
        github: GitHubClient,
    ) -> AcceptFacts:
        if str(caller_id) != str(owner_id):
            raise GateError("only the configured owner may accept a PR")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", full_head_sha):
            raise GateError("accept requires the full head SHA")
        session = self.state.get_session(session_id)
        pipeline = self.state.get_pipeline_run(session_id)
        if session.status is not SessionStatus.PREVIEW_READY:
            raise GateError("session is not preview_ready")
        if pipeline.pr_number != pr_number:
            raise GateError("PR number does not match pipeline state")
        facts = github.get_pr(pr_number)
        if (
            not facts.is_draft
            or facts.state.upper() != "OPEN"
            or facts.head_branch != session.branch
            or facts.head_sha.lower() != full_head_sha.lower()
        ):
            raise GateError("live PR facts do not match owner-provided draft/head SHA")
        checks = github.get_checks(pr_number)
        if not checks.green:
            raise GateError("CI is not green at accept time")
        if not pipeline.preview_url:
            raise GateError("preview URL is missing")
        preview = self.preview_checker.check(pipeline.preview_url)
        if not preview.healthy:
            raise GateError("preview is not healthy at accept time")
        # Perform the external merge before moving durable state to accepted.
        # A failed merge must leave the session retryable instead of making a
        # second accept impossible from an already-accepted state.
        github.merge(number=pr_number, match_head_commit=full_head_sha)
        self.state.update_pipeline_run(
            session_id,
            accepted_by=str(caller_id),
            accepted_head_sha=full_head_sha,
        )
        self.state.transition_session(session_id, SessionStatus.ACCEPTED)
        self.state.transition_session(session_id, SessionStatus.MERGED)
        self.state.record_audit(
            actor=str(caller_id),
            action="owner-accept-merged",
            harness_session_id=session_id,
            details_json=json.dumps(
                {"pr_number": pr_number, "head_sha": full_head_sha},
                ensure_ascii=False,
            ),
        )
        return AcceptFacts(facts, checks, True)

    def reject(self, *, session_id: str, caller_id: str, owner_id: str, feedback: str) -> None:
        if str(caller_id) != str(owner_id):
            raise GateError("only the configured owner may reject")
        if not feedback.strip():
            raise GateError("reject feedback is required")
        session = self.state.get_session(session_id)
        if session.status not in {SessionStatus.PR_OPEN, SessionStatus.CI_PASSED, SessionStatus.PREVIEW_READY}:
            raise GateError("session has no reviewable pipeline state")
        self.state.record_audit(
            actor=str(caller_id),
            action="owner-reject",
            harness_session_id=session_id,
            details_json=json.dumps(
                {"feedback": self.redactor.redact(feedback)[:2000]},
                ensure_ascii=False,
            ),
        )
        self.state.transition_session(session_id, SessionStatus.REVIEW_PENDING)
