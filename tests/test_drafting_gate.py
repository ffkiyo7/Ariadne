from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from ariadne.adapters.claude import (
    DEFAULT_ALLOWED_TOOLS,
    REVIEW_ALLOWED_TOOLS,
    ClaudeAdapter,
    draft_allowed_tools,
)
from ariadne.filesystem import StateLayout
from ariadne.formatting import chunk_message
from ariadne.pipeline.gates import PipelineController
from ariadne.profile import ProjectProfile
from ariadne.state import StateStore


class DraftingToolScopeTests(unittest.TestCase):
    def test_provider_default_tools_cannot_edit_or_write_application_code(self):
        # The S-0007 incident: a drafting turn implemented src/ code because it
        # held bare Edit/Write. The default drafting toolset must be read-only.
        self.assertFalse(any(tool.startswith(("Edit", "Write")) for tool in DEFAULT_ALLOWED_TOOLS))
        self.assertIn("Read", DEFAULT_ALLOWED_TOOLS)

    def test_draft_tools_scope_writes_to_plan_and_task_directories_only(self):
        tools = draft_allowed_tools("docs/plans", "docs/tasks")
        self.assertIn("Edit(docs/plans/**)", tools)
        self.assertIn("Write(docs/tasks/**)", tools)
        # No unscoped write authority leaks in.
        self.assertNotIn("Edit", tools)
        self.assertNotIn("Write", tools)
        self.assertFalse(any(tool in {"Edit", "Write"} for tool in tools))

    def test_claude_drafting_command_forwards_scoped_tools(self):
        adapter = ClaudeAdapter(
            Path("/bin/claude"),
            allowed_models=("m",),
            allowed_tools=draft_allowed_tools("docs/plans", "docs/tasks"),
        )
        command = adapter.new_command(model="m", prompt="draft a plan")
        allowed = next(x for x in command if x.startswith("--allowed-tools="))
        self.assertIn("Edit(docs/plans/**)", allowed)
        self.assertIn("Write(docs/tasks/**)", allowed)
        self.assertNotIn(",Edit,", "," + allowed + ",")
        self.assertNotIn(",Write,", "," + allowed + ",")

    def test_review_tools_remain_strictly_read_only(self):
        self.assertFalse(any(tool.startswith(("Edit", "Write")) for tool in REVIEW_ALLOWED_TOOLS))


class DraftScopeDriftTests(unittest.TestCase):
    def _repo(self, root: Path) -> Path:
        worktree = root / "worktree"
        worktree.mkdir()
        subprocess.run(["git", "init", "-b", "pipeline/S-0001", str(worktree)], check=True, stdout=subprocess.DEVNULL)
        (worktree / "README.md").write_text("fixture\n", encoding="utf-8")
        (worktree / "docs").mkdir()
        (worktree / "docs" / "plans").mkdir()
        (worktree / "docs" / "tasks").mkdir()
        subprocess.run(["git", "-C", str(worktree), "add", "-A"], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(
            [
                "git", "-C", str(worktree), "-c", "user.name=T",
                "-c", "user.email=t@e.invalid", "commit", "-m", "init",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        return worktree

    def test_plan_and_task_writes_are_not_drift_but_code_is(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            worktree = self._repo(root)
            layout = StateLayout.from_state_dir(root / "state").ensure()
            with StateStore(layout.db_path) as state:
                controller = PipelineController(state=state, profile=ProjectProfile.default())
                # A well-behaved drafting turn: only PLAN/TASK markdown.
                (worktree / "docs" / "plans" / "PLAN-daily.md").write_text("# plan\n", encoding="utf-8")
                (worktree / "docs" / "tasks" / "TASK-daily.md").write_text("# task\n", encoding="utf-8")
                self.assertEqual(controller.draft_scope_drift(worktree=worktree), ())
                # A drafting turn that silently implemented code.
                (worktree / "src").mkdir()
                (worktree / "src" / "App.tsx").write_text("code\n", encoding="utf-8")
                drift = controller.draft_scope_drift(worktree=worktree)
                self.assertIn("src/App.tsx", drift)
                self.assertNotIn("docs/plans/PLAN-daily.md", drift)


class ChunkMessageTests(unittest.TestCase):
    def test_splits_on_line_boundaries_not_mid_word(self):
        text = "\n".join(f"line-{i} " + "x" * 50 for i in range(80))
        chunks = chunk_message(text, limit=200)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 200)
        # Reassembling the newline-joined chunks recovers every line.
        self.assertEqual("\n".join(chunks).split(), text.split())

    def test_single_overlong_line_is_hard_cut_as_a_last_resort(self):
        text = "y" * 500
        chunks = chunk_message(text, limit=200)
        self.assertEqual(len(chunks), 3)
        self.assertEqual("".join(chunks), text)

    def test_short_text_is_one_chunk(self):
        self.assertEqual(chunk_message("hello world"), ["hello world"])


if __name__ == "__main__":
    unittest.main()
