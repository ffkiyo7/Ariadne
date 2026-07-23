from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ariadne.filesystem import StateLayout
from ariadne.models import Provider, SessionStatus, TurnKind, TurnState
from ariadne.runner_cli import run_recorded_turn
from ariadne.state import StateStore


class HermesRunnerCliTests(unittest.TestCase):
    def test_hermes_task_uses_recorded_worktree_and_finishes_through_the_durable_runner(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            worktree = root / "worktree"
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
            hermes = root / "hermes"
            hermes.write_text(
                "#!/bin/sh\n"
                "test \"$1\" = -z\n"
                f"test \"$PWD\" = {str(worktree)!r}\n"
                "printf 'fixture change\\n' >> README.md\n"
                "git add README.md\n"
                "git -c user.name='Ariadne Test' -c user.email='ariadne@example.invalid' commit -m fixture\n",
                encoding="utf-8",
            )
            hermes.chmod(0o700)
            task = worktree / "docs" / "tasks" / "TASK-fixture.md"
            task.parent.mkdir(parents=True)
            task.write_text(
                """# Objective
Verify the durable Hermes path.

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
""",
                encoding="utf-8",
            )
            state_dir = root / "state"
            env_path = root / "env"
            env_path.write_text(
                "\n".join(
                    [
                        "DISCORD_TOKEN=not-a-real-token",
                        "DISCORD_ALLOWED_GUILD_ID=123456789012345678",
                        "DISCORD_PARENT_CHANNEL_ID=1529159963526693025",
                        "DISCORD_OWNER_USER_ID=987654321098765432",
                        f"ARIADNE_PROJECT_REPO={worktree}",
                        f"ARIADNE_WORKTREE_ROOT={root / 'worktrees'}",
                        f"ARIADNE_STATE_DIR={state_dir}",
                        f"HERMES_BIN={hermes}",
                        "MAX_CONCURRENT_RUNS=1",
                        "CODEX_DEFAULT_MODEL=codex-a",
                        "CODEX_ALLOWED_MODELS=codex-a",
                        "CLAUDE_DEFAULT_MODEL=claude-a",
                        "CLAUDE_ALLOWED_MODELS=claude-a",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            env_path.chmod(0o600)
            layout = StateLayout.from_state_dir(state_dir).ensure()
            with StateStore(layout.db_path) as state:
                session = state.create_session(
                    source_message_id="source-1",
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
                task_start_head_sha = subprocess.run(
                    ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                    check=True,
                    stdout=subprocess.PIPE,
                    text=True,
                ).stdout.strip()
                turn = state.queue_task_turn(
                    harness_session_id=session.id,
                    provider_session_id=provider.id,
                    task_path=task,
                    task_hash=hashlib.sha256(task.read_bytes()).hexdigest(),
                    task_baseline_json=json.dumps(
                        {
                            "docs/tasks/TASK-fixture.md": "file:"
                            + hashlib.sha256(task.read_bytes()).hexdigest()
                        },
                        sort_keys=True,
                    ),
                    task_start_head_sha=task_start_head_sha,
                )
                state.claim_next()
            result = run_recorded_turn(turn_id=turn.id, env_path=env_path)
            self.assertEqual(result["state"], TurnState.SUCCEEDED.value, result)
            with StateStore(layout.db_path) as state:
                final = state.get_turn(turn.id)
                self.assertEqual(final.execution_kind, TurnKind.HERMES)
                self.assertEqual(final.state, TurnState.SUCCEEDED)
                self.assertTrue(final.result_path and final.result_path.is_file())
                self.assertEqual(state.get_session(session.id).status, SessionStatus.REVIEW_PENDING)
                actions = [row[0] for row in state._connection.execute("SELECT action FROM audit_events")]
                self.assertIn("hermes-start", actions)
                self.assertIn("task-verified", actions)

    def test_review_turn_uses_a_read_only_command_without_replacing_the_implementation_session(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            worktree = root / "worktree"
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
            codex = root / "codex"
            codex.write_text(
                "#!/bin/sh\n"
                "if test \"$1\" = --version; then echo fake-codex; exit 0; fi\n"
                "test \"$1\" = exec\n"
                "test \"$2\" = review\n"
                "test \"$3\" = --base\n"
                "test \"$4\" = origin/main\n"
                "printf '%s\\n' '{\"type\":\"thread.started\",\"thread_id\":\"review-session\"}'\n",
                encoding="utf-8",
            )
            codex.chmod(0o700)
            state_dir = root / "state"
            env_path = root / "env"
            env_path.write_text(
                "\n".join(
                    [
                        "DISCORD_TOKEN=not-a-real-token",
                        "DISCORD_ALLOWED_GUILD_ID=123456789012345678",
                        "DISCORD_PARENT_CHANNEL_ID=1529159963526693025",
                        "DISCORD_OWNER_USER_ID=987654321098765432",
                        f"ARIADNE_PROJECT_REPO={worktree}",
                        f"ARIADNE_WORKTREE_ROOT={root / 'worktrees'}",
                        f"ARIADNE_STATE_DIR={state_dir}",
                        f"CODEX_BIN={codex}",
                        "MAX_CONCURRENT_RUNS=1",
                        "CODEX_DEFAULT_MODEL=codex-a",
                        "CODEX_ALLOWED_MODELS=codex-a",
                        "CLAUDE_DEFAULT_MODEL=claude-a",
                        "CLAUDE_ALLOWED_MODELS=claude-a",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            env_path.chmod(0o600)
            layout = StateLayout.from_state_dir(state_dir).ensure()
            review_prompt = layout.session_dir("S-0001") / "review.md"
            review_prompt.write_text("Review only.\n", encoding="utf-8")
            with StateStore(layout.db_path) as state:
                session = state.create_session(
                    source_message_id="source-review",
                    repo=worktree,
                    worktree=worktree,
                    branch="pipeline/S-0001",
                )
                provider = state.create_provider_session(
                    harness_session_id=session.id,
                    provider=Provider.CODEX,
                    default_model="codex-a",
                    provider_session_id="implementation-session",
                )
                turn = state.create_turn(
                    provider_session_id=provider.id,
                    owner_message_id="review-1",
                    requested_model="codex-a",
                    configured_model="codex-a",
                    input_path=review_prompt,
                    execution_kind=TurnKind.REVIEW,
                )
                state.enqueue_turn(turn.id)
                state.claim_next()
            result = run_recorded_turn(turn_id=turn.id, env_path=env_path)
            self.assertEqual(result["state"], TurnState.SUCCEEDED.value, result)
            with StateStore(layout.db_path) as state:
                self.assertEqual(state.get_turn(turn.id).state, TurnState.SUCCEEDED)
                self.assertEqual(
                    state.get_provider_session(provider.id).provider_session_id,
                    "implementation-session",
                )


if __name__ == "__main__":
    unittest.main()
