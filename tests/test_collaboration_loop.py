from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ariadne.clarification import (
    CLARIFICATION_FILENAME,
    InvalidClarification,
    parse_clarification,
)
from ariadne.decision_pack import (
    build_decision_pack,
    parse_commit_report,
    split_review_sections,
)
from ariadne.filesystem import StateLayout
from ariadne.hermes import HermesExecutor
from ariadne.models import Provider, SessionStatus, TurnKind, TurnState
from ariadne.pipeline.gates import PipelineController
from ariadne.pipeline.task_parser import TaskSpec, parse_task
from ariadne.profile import ProjectProfile, load_profile
from ariadne.retry_context import build_retry_context
from ariadne.runner_cli import run_recorded_turn
from ariadne.state import StateStore


TASK_TEMPLATE = """# Objective
Verify the collaboration loop.

# Allowed files
- `README.md`

# Forbidden zones
Do not alter deployment files.

# Interfaces
No interface changes.

# Definition of done
The command exits successfully.

# Verification commands
```text
python3 -c "print('verified')"
```
"""

CLARIFICATION_TEXT = """## Blocker
The TASK references `src/missing.py`, which does not exist.

## Why the TASK is insufficient
The allowed-files list cannot cover the change the objective requires.

## Options
1. Extend the allowlist to `src/**`.
2. Narrow the objective to README-only wording.

## Recommendation
Option 2: the objective text suggests a docs-only intent.

## Why / Impact
Option 1 would widen write scope beyond what the owner reviewed.
"""


def _git(worktree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ).stdout.strip()


def _init_repo(worktree: Path) -> str:
    worktree.mkdir()
    subprocess.run(["git", "init", "-b", "pipeline/S-0001", str(worktree)], check=True, stdout=subprocess.DEVNULL)
    (worktree / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "README.md"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        [
            "git", "-C", str(worktree), "-c", "user.name=Ariadne Test",
            "-c", "user.email=ariadne@example.invalid", "commit", "-m", "initial",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return _git(worktree, "rev-parse", "HEAD")


def _write_env(root: Path, worktree: Path, hermes: Path | None = None, profile: Path | None = None) -> Path:
    lines = [
        "DISCORD_TOKEN=not-a-real-token",
        "DISCORD_ALLOWED_GUILD_ID=123456789012345678",
        "DISCORD_PARENT_CHANNEL_ID=1529159963526693025",
        "DISCORD_OWNER_USER_ID=987654321098765432",
        f"ARIADNE_PROJECT_REPO={worktree}",
        f"ARIADNE_WORKTREE_ROOT={root / 'worktrees'}",
        f"ARIADNE_STATE_DIR={root / 'state'}",
        "MAX_CONCURRENT_RUNS=1",
        "CODEX_DEFAULT_MODEL=codex-a",
        "CODEX_ALLOWED_MODELS=codex-a",
        "CLAUDE_DEFAULT_MODEL=claude-a",
        "CLAUDE_ALLOWED_MODELS=claude-a",
    ]
    if hermes is not None:
        lines.append(f"HERMES_BIN={hermes}")
    if profile is not None:
        lines.append(f"ARIADNE_PROFILE={profile}")
    env_path = root / "env"
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    return env_path


def _queue_hermes_turn(state: StateStore, worktree: Path, task: Path, provider_id: int, session_id: str):
    baseline = {
        str(task.relative_to(worktree)): "file:" + hashlib.sha256(task.read_bytes()).hexdigest()
    }
    turn = state.queue_task_turn(
        harness_session_id=session_id,
        provider_session_id=provider_id,
        task_path=task,
        task_hash=hashlib.sha256(task.read_bytes()).hexdigest(),
        task_baseline_json=json.dumps(baseline, sort_keys=True),
        task_start_head_sha=_git(worktree, "rev-parse", "HEAD"),
    )
    state.record_audit(
        actor="owner",
        action="task-approved-hermes-queued",
        harness_session_id=session_id,
        turn_id=turn.id,
    )
    state.claim_next()
    return turn


class ClarificationParserTests(unittest.TestCase):
    def test_all_sections_are_required_and_bilingual_aliases_work(self):
        parsed = parse_clarification(CLARIFICATION_TEXT)
        self.assertIn("missing.py", parsed.blocker)
        self.assertIn("Option 2", parsed.recommendation)
        chinese = CLARIFICATION_TEXT.replace("## Blocker", "## 阻塞").replace(
            "## Recommendation", "## 建议"
        )
        self.assertIn("Option 2", parse_clarification(chinese).recommendation)
        with self.assertRaisesRegex(InvalidClarification, "recommendation"):
            parse_clarification(CLARIFICATION_TEXT.split("## Recommendation")[0])


class ClarificationFlowTests(unittest.TestCase):
    def test_worker_question_becomes_a_durable_needs_owner_state(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            worktree = root / "worktree"
            start_sha = _init_repo(worktree)
            hermes = root / "hermes"
            hermes.write_text(
                "#!/bin/sh\n"
                f"cat > {CLARIFICATION_FILENAME} <<'EOF'\n" + CLARIFICATION_TEXT + "EOF\n",
                encoding="utf-8",
            )
            hermes.chmod(0o700)
            task = worktree / "docs" / "tasks" / "TASK-loop.md"
            task.parent.mkdir(parents=True)
            task.write_text(TASK_TEMPLATE, encoding="utf-8")
            env_path = _write_env(root, worktree, hermes=hermes)
            layout = StateLayout.from_state_dir(root / "state").ensure()
            with StateStore(layout.db_path) as state:
                session = state.create_session(
                    source_message_id="clar-1",
                    repo=worktree,
                    worktree=worktree,
                    branch="pipeline/S-0001",
                )
                provider = state.create_provider_session(
                    harness_session_id=session.id,
                    provider=Provider.CODEX,
                    default_model="codex-a",
                )
                state.transition_session(session.id, SessionStatus.QUEUED)
                state.transition_session(session.id, SessionStatus.WAITING_FOR_OWNER)
                state.transition_session(session.id, SessionStatus.PLAN_APPROVED)
                state.create_pipeline_run(session.id, base_sha="a" * 40)
                turn = _queue_hermes_turn(state, worktree, task, provider.id, session.id)
            result = run_recorded_turn(turn_id=turn.id, env_path=env_path)
            self.assertEqual(result["state"], TurnState.FAILED.value, result)
            self.assertTrue(result["error_summary"].startswith("NEEDS_CLARIFICATION"), result)
            # The protocol file is moved out of the worktree; Git history is untouched.
            self.assertFalse((worktree / CLARIFICATION_FILENAME).exists())
            self.assertEqual(_git(worktree, "rev-parse", "HEAD"), start_sha)
            with StateStore(layout.db_path) as state:
                self.assertEqual(state.get_session(session.id).status, SessionStatus.NEEDS_OWNER)
                clarification = state.latest_clarification(session.id)
                assert clarification is not None
                self.assertEqual(clarification.turn_id, turn.id)
                self.assertIn("Option 2", clarification.recommendation)
                self.assertEqual(len(state.list_unposted_clarifications()), 1)
                state.mark_clarification_posted(clarification.id, "111")
                self.assertEqual(state.list_unposted_clarifications(), [])
            copy = layout.session_dir(session.id) / f"clarification-{turn.id}.md"
            self.assertTrue(copy.is_file())

    def test_half_implementation_cannot_disguise_itself_as_a_question(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            worktree = root / "worktree"
            _init_repo(worktree)
            hermes = root / "hermes"
            hermes.write_text(
                "#!/bin/sh\n"
                "printf 'sneaky change\\n' >> README.md\n"
                f"cat > {CLARIFICATION_FILENAME} <<'EOF'\n" + CLARIFICATION_TEXT + "EOF\n",
                encoding="utf-8",
            )
            hermes.chmod(0o700)
            task = worktree / "docs" / "tasks" / "TASK-loop.md"
            task.parent.mkdir(parents=True)
            task.write_text(TASK_TEMPLATE, encoding="utf-8")
            env_path = _write_env(root, worktree, hermes=hermes)
            layout = StateLayout.from_state_dir(root / "state").ensure()
            with StateStore(layout.db_path) as state:
                session = state.create_session(
                    source_message_id="clar-2",
                    repo=worktree,
                    worktree=worktree,
                    branch="pipeline/S-0001",
                )
                provider = state.create_provider_session(
                    harness_session_id=session.id,
                    provider=Provider.CODEX,
                    default_model="codex-a",
                )
                state.transition_session(session.id, SessionStatus.QUEUED)
                state.transition_session(session.id, SessionStatus.WAITING_FOR_OWNER)
                state.transition_session(session.id, SessionStatus.PLAN_APPROVED)
                state.create_pipeline_run(session.id, base_sha="a" * 40)
                turn = _queue_hermes_turn(state, worktree, task, provider.id, session.id)
            result = run_recorded_turn(turn_id=turn.id, env_path=env_path)
            self.assertEqual(result["state"], TurnState.FAILED.value)
            self.assertIn("only worktree change", result["error_summary"])
            with StateStore(layout.db_path) as state:
                self.assertIsNone(state.latest_clarification(session.id))


class RetryContextTests(unittest.TestCase):
    def test_second_round_carries_bounded_previous_evidence(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            worktree = root / "worktree"
            _init_repo(worktree)
            task = worktree / "docs" / "tasks" / "TASK-loop.md"
            task.parent.mkdir(parents=True)
            task.write_text(TASK_TEMPLATE, encoding="utf-8")
            layout = StateLayout.from_state_dir(root / "state").ensure()
            with StateStore(layout.db_path) as state:
                session = state.create_session(
                    source_message_id="retry-1",
                    repo=worktree,
                    worktree=worktree,
                    branch="pipeline/S-0001",
                )
                provider = state.create_provider_session(
                    harness_session_id=session.id,
                    provider=Provider.CODEX,
                    default_model="codex-a",
                )
                state.transition_session(session.id, SessionStatus.QUEUED)
                state.transition_session(session.id, SessionStatus.WAITING_FOR_OWNER)
                state.transition_session(session.id, SessionStatus.PLAN_APPROVED)
                state.create_pipeline_run(session.id, base_sha="a" * 40)

                first = _queue_hermes_turn(state, worktree, task, provider.id, session.id)
                # Round one is on the record: no retry context yet.
                self.assertIsNone(build_retry_context(state, harness_session_id=session.id))
                state.mark_turn_running(first.id)
                state.finalize_turn(
                    first.id,
                    state=TurnState.FAILED,
                    exit_code=1,
                    error_summary="TASK verification failed",
                )
                state.record_audit(
                    actor="ariadne",
                    action="task-verification-failed",
                    harness_session_id=session.id,
                    details_json=json.dumps(
                        {
                            "verification": [
                                {
                                    "command": ["python3", "-m", "unittest"],
                                    "passed": False,
                                    "summary": "AssertionError: expected 2 got 3",
                                }
                            ]
                        }
                    ),
                )
                state.record_audit(
                    actor="owner",
                    action="owner-return-for-revision",
                    harness_session_id=session.id,
                    details_json=json.dumps({"feedback": "不要动缓存层，改为修正计算函数"}),
                )
                second = _queue_hermes_turn(state, worktree, task, provider.id, session.id)
                context = build_retry_context(state, harness_session_id=session.id)
                assert context is not None
                self.assertIn("终止原因", context)
                self.assertIn("AssertionError", context)
                self.assertIn("不要动缓存层", context)
                self.assertLessEqual(len(context), 3200)
                del second
                # The evidence reaches the actual Hermes prompt.
                executor = HermesExecutor(executable=root / "hermes-bin")
                command = executor.build_command(
                    task=parse_task(task),
                    worktree=worktree,
                    branch="pipeline/S-0001",
                    retry_context=context,
                )
                prompt = command.argv[2]
                self.assertIn("Previous round evidence", prompt)
                self.assertIn("AssertionError", prompt)


class DecisionPackTests(unittest.TestCase):
    def test_commit_report_parsing_and_mismatch_detection(self):
        subject, body, claimed, present = parse_commit_report(
            "fix: correct stat rounding\n\n"
            "Rounded at display time instead of storage, because storage is shared.\n"
            "Risk: none expected outside the stats page.\n\n"
            "Files:\n- src/stats.ts\n- src/display.ts\n"
        )
        self.assertEqual(subject, "fix: correct stat rounding")
        self.assertIn("display time", body)
        self.assertTrue(present)
        self.assertEqual(claimed, ("src/stats.ts", "src/display.ts"))
        sections, verdict = split_review_sections(
            "## 结论\nPASS：改动与 TASK 一致。\n\n## 行为变化\n无\n\n## 风险与遗留\n无\n"
        )
        self.assertEqual(verdict, "PASS")
        self.assertEqual(sections[0][0], "结论")

    def test_pack_flags_narrative_that_disagrees_with_git(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            worktree = root / "worktree"
            start_sha = _init_repo(worktree)
            (worktree / "README.md").write_text("fixture\nchanged\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(worktree), "add", "README.md"], check=True, stdout=subprocess.DEVNULL)
            subprocess.run(
                [
                    "git", "-C", str(worktree), "-c", "user.name=Ariadne Test",
                    "-c", "user.email=ariadne@example.invalid", "commit", "-m",
                    "docs: update readme\n\nUpdated the fixture wording.\n\nFiles:\n- README.md\n- docs/missing.md\n",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            layout = StateLayout.from_state_dir(root / "state").ensure()
            with StateStore(layout.db_path) as state:
                session = state.create_session(
                    source_message_id="pack-1",
                    repo=worktree,
                    worktree=worktree,
                    branch="pipeline/S-0001",
                )
                provider = state.create_provider_session(
                    harness_session_id=session.id,
                    provider=Provider.CLAUDE,
                    default_model="claude-a",
                )
                state.create_pipeline_run(
                    session.id, base_sha="a" * 40, task_start_head_sha=start_sha
                )
                hermes_turn = state.create_turn(
                    provider_session_id=provider.id,
                    owner_message_id="hermes-1",
                    requested_model="hermes",
                    configured_model="hermes",
                    execution_kind=TurnKind.HERMES,
                )
                state.update_pipeline_run(session.id, task_turn_id=hermes_turn.id)
                review_turn = state.create_turn(
                    provider_session_id=provider.id,
                    owner_message_id="review-1",
                    requested_model="claude-a",
                    configured_model="claude-a",
                    execution_kind=TurnKind.REVIEW,
                )
                transcript = layout.session_dir(session.id) / "review.jsonl"
                final_review = (
                    "## 结论\nPASS：与 TASK 一致。\n\n## 改动核对\n只改了 README。\n"
                )
                transcript.write_text(
                    json.dumps(
                        {
                            "ts": "t",
                            "stream": "stdout",
                            "line": json.dumps({"type": "result", "result": final_review}),
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                state.enqueue_turn(review_turn.id)
                state.claim_next()
                state.mark_turn_running(review_turn.id)
                state.finalize_turn(
                    review_turn.id,
                    state=TurnState.SUCCEEDED,
                    exit_code=0,
                    sanitized_path=transcript,
                )
                pack = build_decision_pack(
                    state,
                    harness_session_id=session.id,
                    profile=ProjectProfile.default(),
                )
            self.assertEqual(pack.reviewer_verdict, "PASS")
            self.assertEqual(pack.implementer_subject, "docs: update readme")
            self.assertEqual([path for _, path in pack.committed], ["README.md"])
            self.assertTrue(any("docs/missing.md" in item for item in pack.mismatches))


class KnowledgeScopeTests(unittest.TestCase):
    def test_knowledge_file_is_loaded_and_added_to_task_scope(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            profile_path = root / "profile.toml"
            profile_path.write_text(
                '[project]\nid = "loop-test"\n\n[workflow]\nknowledge_file = "docs/dev-knowledge.md"\n',
                encoding="utf-8",
            )
            profile = load_profile(profile_path)
            assert profile.knowledge_file is not None
            self.assertEqual(profile.knowledge_file.as_posix(), "docs/dev-knowledge.md")
            layout = StateLayout.from_state_dir(root / "state").ensure()
            with StateStore(layout.db_path) as state:
                controller = PipelineController(state=state, profile=profile)
                task = TaskSpec(
                    path=root / "TASK.md",
                    objective="o",
                    allowed_files=("README.md",),
                    forbidden_zones="none",
                    interfaces="none",
                    definition_of_done="done",
                    verification_commands=("true",),
                )
                scoped = controller.scoped_task(task)
                self.assertTrue(scoped.allows_path("docs/dev-knowledge.md"))
                self.assertTrue(scoped.allows_path("README.md"))
                self.assertFalse(scoped.allows_path("AGENTS.md"))
                # The augmented scope reaches the worker prompt and mentions the journal.
                command = HermesExecutor(executable=root / "hermes-bin").build_command(
                    task=scoped,
                    worktree=root,
                    branch="pipeline/S-0001",
                    knowledge_file=profile.knowledge_file.as_posix(),
                )
                self.assertIn("docs/dev-knowledge.md", command.argv[2])
                self.assertIn("Project knowledge", command.argv[2])


if __name__ == "__main__":
    unittest.main()
