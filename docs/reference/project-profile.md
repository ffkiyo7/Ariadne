# Project profile contract

A profile is versioned TOML.  It contains portable repository facts; paths to
the target checkout, state directory, executables, Discord IDs, and credentials
are supplied only through a private environment file.

```toml
[project]
id = "example-project"

[git]
remote = "origin"
base_branch = "main"
branch_prefix = "ariadne/"

[workflow]
plan_directory = "docs/plans"
task_directory = "docs/tasks"
protected_sibling_checkouts = ["example-project-maintenance"]
preview_required = true
```

`id` is lowercase letters, digits, and hyphens.  `plan_directory` and
`task_directory` must be safe paths relative to the session worktree.
`protected_sibling_checkouts` names direct siblings of the target checkout;
they are rejected as worktree destinations.  A branch must be exactly
`<branch_prefix>S-####` before Ariadne will push it or create a Draft PR.

Private environment example (with placeholders only):

```dotenv
ARIADNE_PROFILE=/absolute/path/to/Ariadne/profiles/example.toml
ARIADNE_PROJECT_REPO=/absolute/path/to/target-project
ARIADNE_WORKTREE_ROOT=/absolute/path/to/target-project-worktrees
ARIADNE_STATE_DIR=/absolute/path/to/private-state
HERMES_BIN=/absolute/path/to/hermes

DISCORD_TOKEN=<private>
DISCORD_ALLOWED_GUILD_ID=<private>
DISCORD_PARENT_CHANNEL_ID=<private>
DISCORD_OWNER_USER_ID=<private>
CODEX_BIN=/absolute/path/to/codex-wrapper
CLAUDE_BIN=/absolute/path/to/claude
MAX_CONCURRENT_RUNS=1
```

For one controlled LuxrayKit cutover, Ariadne also reads the legacy
`HARNESS_REPO`, `WORKTREE_ROOT`, and `HARNESS_STATE_DIR` names.  New
installations should use the `ARIADNE_*` names above.
