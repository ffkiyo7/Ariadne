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
# Used only for Ariadne's non-interactive `gh` calls (Draft PR, CI, !accept).
ARIADNE_GITHUB_TOKEN=<private>
CODEX_BIN=/absolute/path/to/codex-wrapper
# Default false. Enable only after reviewing the egress trade-off documented below.
CODEX_WORKSPACE_NETWORK_ACCESS=false
CLAUDE_BIN=/absolute/path/to/claude
MAX_CONCURRENT_RUNS=1
```

`CODEX_WORKSPACE_NETWORK_ACCESS` is an explicit, per-Ariadne-runtime escape
hatch for hosts where Codex's default network-isolated workspace sandbox cannot
start.  It keeps filesystem writes limited to the session worktree, but it lets
model-issued commands use network access.  Leave it `false` by default; set it
to `true` only for a reviewed runtime with appropriate host-level egress policy.

`ARIADNE_GITHUB_TOKEN` is never inherited by Codex, Claude, or Hermes turns.
It is injected only into Ariadne's short-lived `gh` subprocesses.  A token with
access to the target repository is required for a non-interactive service that
opens Draft PRs or checks CI; an interactive developer installation may instead
use its existing `gh auth login` session.

For one controlled LuxrayKit cutover, Ariadne also reads the legacy
`HARNESS_REPO`, `WORKTREE_ROOT`, and `HARNESS_STATE_DIR` names.  New
installations should use the `ARIADNE_*` names above.
