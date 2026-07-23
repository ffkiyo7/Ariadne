from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ariadne.hermes import HermesExecutionError, HermesExecutor
from ariadne.pipeline.task_parser import TaskSpec


class HermesExecutorTests(unittest.TestCase):
    def test_build_uses_the_fixed_executable_and_direct_run_is_rejected(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            executable = root / "hermes"
            executable.write_text(
                "#!/bin/sh\n"
                "test \"$1\" = -z\n"
                "test \"$PWD\" = '" + str(root / "worktree") + "'\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            worktree = root / "worktree"
            worktree.mkdir()
            task = TaskSpec(
                path=root / "TASK.md",
                objective="docs-only",
                allowed_files=("docs/**",),
                forbidden_zones="no production files",
                interfaces="keep interfaces",
                definition_of_done="tests pass",
                verification_commands=("python -m unittest",),
            )
            executor = HermesExecutor(executable=executable)
            command = executor.build_command(task=task, worktree=worktree, branch="pipeline/S-0001")
            self.assertEqual(command.argv[:2], (str(executable), "-z"))
            with self.assertRaises(HermesExecutionError):
                executor.run(command)


if __name__ == "__main__":
    unittest.main()
