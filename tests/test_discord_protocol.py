from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

from ariadne.adapters.base import AdapterEvent
from ariadne.commands import CommandParseError, parse_control
from ariadne.config import Config
from ariadne.discord_bot import bot as discord_bot
from ariadne.discord_bot.bot import DiscordHarnessService
from ariadne.filesystem import StateLayout
from ariadne.models import Provider, SessionStatus, TurnKind, TurnState
from ariadne.scheduler import Coordinator
from ariadne.state import StateError, StateStore
from ariadne.systemd import FakeSystemdUserClient
from ariadne.worktrees import WorktreeInfo


class FakeWorktrees:
    def __init__(self, root: Path):
        self.root = root
        self.created: list[str] = []

    def create(self, session_id: str, *, branch: str | None = None):
        path = self.root / session_id
        path.mkdir(parents=True, exist_ok=False)
        selected_branch = branch or f"pipeline/{session_id}"
        subprocess.run(
            ["git", "init", "-b", selected_branch, str(path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (path / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(path), "add", "README.md"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                "git", "-C", str(path), "-c", "user.name=Ariadne Test",
                "-c", "user.email=ariadne@example.invalid", "commit", "-m", "initial",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.created.append(session_id)
        return WorktreeInfo(
            session_id=session_id,
            path=path,
            branch=selected_branch,
            base_sha="0" * 40,
        )


class CommandTests(unittest.TestCase):
    def test_control_parser_is_strict_and_never_shells(self):
        self.assertEqual(parse_control("!status").name, "status")
        self.assertEqual(parse_control("!provider claude").args, ("claude",))
        self.assertEqual(parse_control("!effort xhigh").args, ("xhigh",))
        self.assertEqual(parse_control("!reject please review this").args, ("please", "review", "this"))
        self.assertEqual(parse_control("normal owner text"), None)
        with self.assertRaises(CommandParseError):
            parse_control("!unknown $(touch /tmp/nope)")
        with self.assertRaises(CommandParseError):
            parse_control("!accept 12 deadbeef")

    @unittest.skipIf(discord_bot.commands is None, "discord.py is not installed")
    def test_slash_handler_does_not_override_discord_gateway_dispatch(self):
        self.assertIsNot(
            discord_bot.DiscordHarnessBot.dispatch,
            discord_bot.DiscordHarnessBot.dispatch_command,
        )

    @unittest.skipIf(discord_bot.commands is None, "discord.py is not installed")
    def test_completed_turn_event_is_not_posted_as_thread_copy(self):
        event = AdapterEvent(kind="turn_finished", provider=Provider.CODEX, raw_type="result")
        self.assertIsNone(discord_bot.DiscordHarnessBot._visible_event_text(event))


class DiscordServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="/tmp")
        root = Path(self.temp.name)
        self.root = root
        env = {
            "DISCORD_TOKEN": "discord-secret",
            "DISCORD_ALLOWED_GUILD_ID": "123456789012345678",
            "DISCORD_PARENT_CHANNEL_ID": "1529159963526693025",
            "DISCORD_OWNER_USER_ID": "987654321098765432",
            "HARNESS_REPO": str(root / "repo"),
            "WORKTREE_ROOT": str(root / "worktrees"),
            "HARNESS_STATE_DIR": str(root / "state"),
            "MAX_CONCURRENT_RUNS": "1",
            "CODEX_DEFAULT_MODEL": "codex-a",
            "CODEX_ALLOWED_MODELS": "codex-a,codex-b",
            "CLAUDE_DEFAULT_MODEL": "claude-opus-4-8",
            "CLAUDE_ALLOWED_MODELS": "claude-opus-4-8",
        }
        self.config = Config.from_env(env)
        self.layout = StateLayout.from_state_dir(self.config.state_dir).ensure()
        self.state = StateStore(self.layout.db_path)
        self.coordinator = Coordinator(
            state=self.state,
            layout=self.layout,
            systemd=FakeSystemdUserClient(),
            command_builder=lambda turn: ["fake", turn.id],
        )
        self.worktrees = FakeWorktrees(self.config.worktree_root)
        self.service = DiscordHarnessService(
            config=self.config,
            state=self.state,
            layout=self.layout,
            coordinator=self.coordinator,
            worktrees=self.worktrees,
        )

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_source_message_creates_thread_ready_session_and_deduplicates(self):
        first = self.service.start_from_source(
            source_message_id="source-1",
            source_content="Please inspect the docs.",
            provider_name="codex",
        )
        self.assertTrue(first.created)
        self.assertEqual(self.worktrees.created, [first.session_id])
        second = self.service.start_from_source(
            source_message_id="source-1",
            source_content="same source",
            provider_name="claude",
        )
        self.assertFalse(second.created)
        self.assertEqual(second.session_id, first.session_id)
        self.assertEqual(len(self.state.list_turns(first.session_id)), 1)
        card = self.service.status_card(first.session_id)
        text = "\n".join([card.title, card.description, *[f"{k}:{v}" for k, v in card.fields]])
        self.assertIn("requested model", text)
        self.assertNotIn("discord-secret", text)
        pipeline = self.state.get_pipeline_run(first.session_id)
        self.assertEqual(pipeline.base_sha, "0" * 40)

    def test_dispatch_waits_for_provider_choice_and_is_idempotent(self):
        dispatch = self.service.create_dispatch(
            owner_user_id=self.config.owner_user_id or "",
            guild_id=self.config.allowed_guild_id or "",
            channel_id=self.config.parent_channel_id or "",
            task="Draft the release notes.",
        )
        self.assertEqual(dispatch.status.value, "pending")
        self.assertEqual(self.worktrees.created, [])
        self.assertEqual(self.state.list_sessions(), [])

        edited = self.state.update_dispatch_task(dispatch.id, "Draft the corrected release notes.")
        self.assertEqual(edited.task, "Draft the corrected release notes.")

        started = self.service.start_from_dispatch(dispatch_id=dispatch.id, provider_name="codex")
        self.assertTrue(started.created)
        self.assertEqual(self.worktrees.created, [started.session_id])
        completed = self.state.get_dispatch(dispatch.id)
        self.assertEqual(completed.status.value, "started")
        self.assertEqual(completed.provider.value if completed.provider else None, "codex")
        self.assertEqual(completed.harness_session_id, started.session_id)
        provider = self.state.list_provider_sessions(started.session_id)[0]
        self.assertFalse(provider.configuration_locked)
        self.assertEqual(self.state.list_turns(started.session_id), [])
        with self.assertRaises(StateError):
            self.service.enqueue_owner_message(
                session_id=started.session_id,
                owner_message_id="before-config",
                content="This must not be queued.",
            )

        self.service.choose_initial_model(session_id=started.session_id, model="codex-b")
        self.service.choose_initial_effort(session_id=started.session_id, effort="xhigh")
        locked, initial_turn, created = self.service.lock_initial_configuration(
            session_id=started.session_id
        )
        self.assertTrue(created)
        self.assertTrue(locked.configuration_locked)
        self.assertEqual(locked.default_model, "codex-b")
        self.assertEqual(locked.default_effort, "xhigh")
        self.assertEqual(initial_turn.configured_model, "codex-b")
        self.assertEqual(initial_turn.configured_effort, "xhigh")
        self.assertEqual(self.state.queue_position(initial_turn.id), 1)
        locked_again, same_turn, created_again = self.service.lock_initial_configuration(
            session_id=started.session_id
        )
        self.assertFalse(created_again)
        self.assertEqual(locked_again.id, locked.id)
        self.assertEqual(same_turn.id, initial_turn.id)
        with self.assertRaises(StateError):
            self.service.choose_initial_effort(session_id=started.session_id, effort="high")

        duplicate = self.service.start_from_dispatch(dispatch_id=dispatch.id, provider_name="claude")
        self.assertFalse(duplicate.created)
        self.assertEqual(duplicate.session_id, started.session_id)
        self.assertEqual(len(self.state.list_provider_sessions(started.session_id)), 1)
        with self.assertRaises(StateError):
            self.state.update_dispatch_task(dispatch.id, "Too late to edit.")

    @unittest.skipIf(discord_bot.commands is None, "discord.py is not installed")
    def test_initial_configuration_view_has_no_claude_model_select(self):
        dispatch = self.service.create_dispatch(
            owner_user_id=self.config.owner_user_id or "",
            guild_id=self.config.allowed_guild_id or "",
            channel_id=self.config.parent_channel_id or "",
            task="Configure views.",
        )
        codex_start = self.service.start_from_dispatch(dispatch_id=dispatch.id, provider_name="codex")
        codex_provider = self.state.list_provider_sessions(codex_start.session_id)[0]
        codex_view = discord_bot.SessionConfigurationView(
            bot=SimpleNamespace(service=self.service),
            session_id=codex_start.session_id,
            provider=codex_provider,
        )
        self.assertTrue(codex_view.is_persistent())
        self.assertEqual(
            [item.custom_id.rsplit(":", 1)[-1] for item in codex_view.children],
            ["model", "effort", "confirm"],
        )

        claude_dispatch = self.service.create_dispatch(
            owner_user_id=self.config.owner_user_id or "",
            guild_id=self.config.allowed_guild_id or "",
            channel_id=self.config.parent_channel_id or "",
            task="Configure Claude view.",
        )
        claude_start = self.service.start_from_dispatch(
            dispatch_id=claude_dispatch.id, provider_name="claude"
        )
        claude_provider = self.state.list_provider_sessions(claude_start.session_id)[0]
        claude_view = discord_bot.SessionConfigurationView(
            bot=SimpleNamespace(service=self.service),
            session_id=claude_start.session_id,
            provider=claude_provider,
        )
        self.assertTrue(claude_view.is_persistent())
        self.assertEqual(
            [item.custom_id.rsplit(":", 1)[-1] for item in claude_view.children],
            ["effort", "confirm"],
        )

    def test_owner_can_register_and_approve_a_plan_from_the_thread(self):
        start = self.service.start_from_source(
            source_message_id="source-1",
            source_content="Start",
            provider_name="codex",
        )
        plan = self.worktrees.root / start.session_id / "docs" / "plans" / "PLAN-docs.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# PLAN\n\nSafe docs-only change.\n", encoding="utf-8")
        turn = self.state.list_turns(start.session_id)[0]
        self.state.claim_next()
        self.state.finalize_turn(turn.id, state=TurnState.SUCCEEDED, exit_code=0)
        response = self.service.handle_control(
            session_id=start.session_id,
            command=parse_control("!approve PLAN-docs"),
        )
        self.assertIn("已批准", response)
        self.assertEqual(self.state.get_session(start.session_id).status.value, "plan_approved")

    def test_owner_approved_task_becomes_a_durable_hermes_turn(self):
        start = self.service.start_from_source(
            source_message_id="source-task",
            source_content="Start",
            provider_name="codex",
        )
        worktree = self.worktrees.root / start.session_id
        plan = worktree / "docs" / "plans" / "PLAN-safe.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# PLAN\n\nSafe docs-only change.\n", encoding="utf-8")
        initial = self.state.list_turns(start.session_id)[0]
        self.state.claim_next()
        self.state.finalize_turn(initial.id, state=TurnState.SUCCEEDED, exit_code=0)
        self.service.handle_control(session_id=start.session_id, command=parse_control("!approve PLAN-safe"))
        task = worktree / "docs" / "tasks" / "TASK-safe.md"
        task.parent.mkdir(parents=True)
        task.write_text(
            """# 目标
更新一个文档。

# 允许改动
- `docs/README.md`

# 禁区
不得改动生产配置。

# 接口
只做文档修改。

# DoD
文档已更新。

# 验证命令
```text
python3 -c "print('ok')"
```
""",
            encoding="utf-8",
        )
        response = self.service.handle_control(
            session_id=start.session_id,
            command=parse_control("!task TASK-safe"),
        )
        self.assertIn("Hermes", response)
        turn = self.state.list_turns(start.session_id)[-1]
        self.assertEqual(turn.execution_kind, TurnKind.HERMES)
        self.assertEqual(self.state.get_session(start.session_id).status, SessionStatus.TASK_RUNNING)
        pipeline = self.state.get_pipeline_run(start.session_id)
        self.assertEqual(pipeline.task_turn_id, turn.id)

    def test_failed_review_can_retry_without_reexecuting_hermes(self):
        start = self.service.start_from_source(
            source_message_id="source-review-retry",
            source_content="Start",
            provider_name="codex",
        )
        worktree = self.worktrees.root / start.session_id
        plan = worktree / "docs" / "plans" / "PLAN-review.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# PLAN\n\nSafe docs-only change.\n", encoding="utf-8")
        initial = self.state.list_turns(start.session_id)[0]
        self.state.claim_next()
        self.state.finalize_turn(initial.id, state=TurnState.SUCCEEDED, exit_code=0)
        self.service.handle_control(session_id=start.session_id, command=parse_control("!approve PLAN-review"))
        task = worktree / "docs" / "tasks" / "TASK-review.md"
        task.parent.mkdir(parents=True)
        task.write_text(
            """# Objective
Update the documentation.

# Allowed files
- `README.md`

# Forbidden zones
Do not push or merge.

# Interfaces
None.

# Definition of done
The README is updated.

# Verification commands
```text
test -f README.md
```
""",
            encoding="utf-8",
        )
        self.service.handle_control(session_id=start.session_id, command=parse_control("!task TASK-review"))
        hermes = self.state.list_turns(start.session_id)[-1]
        self.state.claim_next()
        self.state.finalize_turn(hermes.id, state=TurnState.SUCCEEDED, exit_code=0)
        failed_review = self.service.request_review(session_id=start.session_id)
        self.state.claim_next()
        self.state.finalize_turn(failed_review.id, state=TurnState.FAILED, exit_code=2)
        self.assertEqual(self.state.get_session(start.session_id).status, SessionStatus.NEEDS_OWNER)

        retry = self.service.request_review(session_id=start.session_id)
        self.assertEqual(retry.execution_kind, TurnKind.REVIEW)
        self.assertEqual(self.state.get_session(start.session_id).status, SessionStatus.REVIEW_PENDING)
        self.assertEqual(
            [turn.execution_kind for turn in self.state.list_turns(start.session_id)].count(TurnKind.HERMES),
            1,
        )

    def test_owner_message_is_clarification_and_only_a_button_advances_the_plan(self):
        start = self.service.start_from_source(
            source_message_id="source-plan-gate",
            source_content="Add a daily-pick feature.",
            provider_name="claude",
        )
        session_id = start.session_id
        worktree = self.worktrees.root / session_id
        # Drive the initial drafting turn to completion -> waiting_for_owner.
        initial = self.state.list_turns(session_id)[0]
        self.state.claim_next()
        self.state.finalize_turn(initial.id, state=TurnState.SUCCEEDED, exit_code=0)
        self.assertEqual(self.state.get_session(session_id).status, SessionStatus.WAITING_FOR_OWNER)

        # No PLAN file yet: nothing is approvable and the card says so.
        self.assertEqual(self.service.detect_plan_candidates(session_id), ())
        card = self.service.status_card(session_id)
        card_text = "\n".join(f"{k}:{v}" for k, v in card.fields)
        self.assertIn("未检测到新 PLAN", card_text)

        # An owner chat message must NOT advance the phase; it enqueues a
        # drafting/clarification turn and the session stays waiting_for_owner.
        turn = self.service.enqueue_owner_message(
            session_id=session_id,
            owner_message_id="owner-clarify-1",
            content="批准，进入实现吧",
        )
        # The message queues a drafting turn (QUEUED) but never crosses into an
        # approval phase; a conversational "批准" is not a gate.
        self.assertEqual(self.state.get_session(session_id).status, SessionStatus.QUEUED)
        prompt = turn.input_path.read_text(encoding="utf-8")
        self.assertIn("clarification", prompt.lower())
        self.assertIn("NOT an approval", prompt)
        self.state.claim_next()
        self.state.finalize_turn(turn.id, state=TurnState.SUCCEEDED, exit_code=0)
        self.assertEqual(self.state.get_session(session_id).status, SessionStatus.WAITING_FOR_OWNER)

        # The drafting turn produces a PLAN file: now it is an approval candidate.
        plan_dir = worktree / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        (plan_dir / "PLAN-daily-pick.md").write_text("# PLAN\n\nDaily pick.\n", encoding="utf-8")
        # A forced status refresh (which fires on turn finalization) invalidates
        # the short detection cache; mirror that here.
        self.service.invalidate_candidates(session_id)
        candidates = self.service.detect_plan_candidates(session_id)
        self.assertEqual([Path(p).stem for p in candidates], ["PLAN-daily-pick"])

        # The explicit button action is the only thing that advances the phase.
        approved = self.service.approve_plan(session_id=session_id, plan_selector="PLAN-daily-pick")
        self.assertEqual(approved, "PLAN-daily-pick.md")
        self.assertEqual(self.state.get_session(session_id).status, SessionStatus.PLAN_APPROVED)

    def test_review_pending_can_be_returned_for_revision_and_feeds_the_next_round(self):
        start = self.service.start_from_source(
            source_message_id="source-revise",
            source_content="Start",
            provider_name="codex",
        )
        worktree = self.worktrees.root / start.session_id
        plan = worktree / "docs" / "plans" / "PLAN-revise.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# PLAN\n\nSafe docs-only change.\n", encoding="utf-8")
        initial = self.state.list_turns(start.session_id)[0]
        self.state.claim_next()
        self.state.finalize_turn(initial.id, state=TurnState.SUCCEEDED, exit_code=0)
        self.service.handle_control(session_id=start.session_id, command=parse_control("!approve PLAN-revise"))
        task = worktree / "docs" / "tasks" / "TASK-revise.md"
        task.parent.mkdir(parents=True)
        task.write_text(
            """# Objective
Update the documentation.

# Allowed files
- `README.md`

# Forbidden zones
Do not push or merge.

# Interfaces
None.

# Definition of done
The README is updated.

# Verification commands
```text
test -f README.md
```
""",
            encoding="utf-8",
        )
        self.service.handle_control(session_id=start.session_id, command=parse_control("!task TASK-revise"))
        hermes = self.state.list_turns(start.session_id)[-1]
        self.state.claim_next()
        self.state.finalize_turn(hermes.id, state=TurnState.SUCCEEDED, exit_code=0)
        review = self.service.request_review(session_id=start.session_id)
        # The structured review contract is part of the request pack.
        request_text = review.input_path.read_text(encoding="utf-8")
        for heading in ("## 结论", "## 改动核对", "## TASK 充分性"):
            self.assertIn(heading, request_text)
        self.state.claim_next()
        self.state.finalize_turn(review.id, state=TurnState.SUCCEEDED, exit_code=0)

        with self.assertRaises(discord_bot.GateError):
            self.service.return_for_revision(session_id=start.session_id, feedback="   ")
        self.service.return_for_revision(
            session_id=start.session_id, feedback="review 指出边界条件遗漏，请改用查表实现"
        )
        self.assertEqual(self.state.get_session(start.session_id).status, SessionStatus.NEEDS_OWNER)
        # Re-approval is possible again, and the feedback reaches the next round.
        self.service.handle_control(session_id=start.session_id, command=parse_control("!task TASK-revise"))
        from ariadne.retry_context import build_retry_context

        context = build_retry_context(self.state, harness_session_id=start.session_id)
        assert context is not None
        self.assertIn("查表实现", context)

    def test_owner_message_enqueues_one_turn_after_configuration_is_fixed(self):
        start = self.service.start_from_source(
            source_message_id="source-1",
            source_content="Start",
            provider_name="codex",
        )
        turn = self.service.enqueue_owner_message(
            session_id=start.session_id,
            owner_message_id="owner-2",
            content="Follow up",
        )
        self.assertEqual(self.state.queue_position(turn.id), 2)
        with self.assertRaises(StateError):
            self.service.handle_control(
                session_id=start.session_id,
                command=parse_control("!provider claude"),
            )
        self.assertEqual(len(self.state.list_provider_sessions(start.session_id)), 1)

    def test_claude_configuration_exposes_effort_but_keeps_opus_fixed(self):
        dispatch = self.service.create_dispatch(
            owner_user_id=self.config.owner_user_id or "",
            guild_id=self.config.allowed_guild_id or "",
            channel_id=self.config.parent_channel_id or "",
            task="Draft the release notes with Claude.",
        )
        start = self.service.start_from_dispatch(dispatch_id=dispatch.id, provider_name="claude")
        provider = self.state.list_provider_sessions(start.session_id)[0]
        self.assertEqual(provider.default_model, "claude-opus-4-8")
        with self.assertRaises(StateError):
            self.service.choose_initial_model(session_id=start.session_id, model="claude-opus-4-8")
        self.service.choose_initial_effort(session_id=start.session_id, effort="max")
        locked, turn, created = self.service.lock_initial_configuration(session_id=start.session_id)
        self.assertTrue(created)
        self.assertTrue(locked.configuration_locked)
        self.assertEqual(turn.configured_model, "claude-opus-4-8")
        self.assertEqual(turn.configured_effort, "max")

    def test_stop_cancels_a_queued_turn_without_starting_a_process(self):
        start = self.service.start_from_source(
            source_message_id="source-1",
            source_content="Start",
            provider_name="codex",
        )
        response = self.service.handle_control(
            session_id=start.session_id,
            command=parse_control("!stop"),
        )
        self.assertIn("已取消", response)
        self.assertEqual(self.state.list_turns(start.session_id)[0].state.value, "cancelled")


if __name__ == "__main__":
    unittest.main()
