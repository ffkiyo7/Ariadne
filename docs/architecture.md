# Ariadne architecture

## Safety model

Ariadne is an owner-operated workflow, not an autonomous deployment or merge
agent.  It permits one actual CLI turn globally.  Each target project gets an
independent Git worktree and branch; the service checkout, a protected sibling
checkout, and `main` are never model write targets.

Provider configuration is selected once on the initial Discord Thread card:
Codex selects an allowlisted model and reasoning effort; Claude exposes its
configured model and an allowlisted effort.  Text commands cannot change the
provider, model, or effort afterwards.  This is deliberate: a Thread is a
reproducible provider-session boundary, not a free-form model switchboard.

Codex runs are non-interactive after those owner gates, so their CLI command
uses `approval_policy="never"` while retaining the declared sandbox mode.  It
prevents an unavailable terminal approval prompt from becoming an implicit
success path; it does not grant filesystem or network access beyond the
configured sandbox.

## Durable execution

Every actual Codex, Claude, Hermes, or review process is a row in SQLite and a
`systemd --user` transient unit.  The runner owns the subprocess, global lock,
worktree lock, raw transcript, redacted Discord transcript, and terminal
result.  On restart, the coordinator either finds the live unit, imports the
terminal result, or marks the turn interrupted; it never assumes an in-memory
pipe can be reconnected.

Hermes code work is a distinct `hermes` turn type.  It is **not** permitted to
use the Hermes HTTP API or a direct subprocess from the Discord bot.  The
runner starts the fixed `HERMES_BIN -z` command with `cwd=<session-worktree>`.
On a zero exit it verifies the approved TASK hash, changed-file allowlist, and
verification commands while the same locks are still held.  Hermes must leave
exactly one local commit that descends from the approved HEAD; the commit and
any remaining worktree changes are independently checked against the TASK
allowlist.  A TASK may not prohibit that required local commit, while push and
merge remain owner-gated.  Frozen, unchanged PLAN/TASK files may remain local
and untracked; every other dirty path blocks the later push.  A failed
post-execution gate leaves the session in `needs_owner`, not in review.

Review is deliberately a fresh, read-only turn rather than a resume of the
implementation conversation: Codex uses `exec review` with its read-only disk
policy, while Claude receives no `Edit` or `Write` tool.  Review output is
transcribed but its provider-session id is never adopted as a later editing
session.

A Hermes turn has a second legitimate terminal outcome besides one commit:
a structured `ARIADNE-CLARIFICATION.md` request with no other change, which
lands the session in `needs_owner` with a question card in the Thread.  A
retried TASK automatically receives bounded previous-round evidence, and a
completed review can be sent back for TASK revision with owner feedback.
When a review completes, the bot posts a decision pack whose primary content
is the implementer's commit self-report and the reviewer's structured
conclusion, with Git/verification facts cross-checked against the narrative
and mismatches flagged.  See
[collaboration-loop.md](collaboration-loop.md) for the mechanisms and their
rationale.

## Pipeline gates

```text
waiting_for_owner
  -> plan_approved
  -> task_running            (owner presses the TASK status-card action)
  -> review_pending          (Hermes + deterministic TASK gate succeeded)
  -> review_pending          (strong-model review completes; owner confirms it)
  -> pr_open                 (clean branch is pushed, then Draft PR is created)
  -> ci_passed
  -> preview_ready           (only when the project profile requires preview)
  -> accepted -> merged      (explicit owner !accept with current full head SHA)
```

The status card exposes only the action valid for the persisted state:
approve/retry TASK, retry a failed review, request review, confirm review, create Draft PR, check CI,
or submit a preview URL.  This does not replace owner judgment: the owner reads
the review transcript and explicitly confirms it before a branch is pushed.
The bot redraws persisted cards after every gateway reconnect, so a restart
cannot leave the owner with a stale action surface.  Its scheduler also
reconciles a card after a persisted turn-state transition even when no later
provider transcript line is available.

## Data ownership

| Data | Authority |
| --- | --- |
| PLAN, TASK, review prose | Target project worktree Markdown |
| Sessions, queue, units, cursor, gate facts | Private Ariadne SQLite state |
| Provider output | Private raw/transcript files |
| Git remote/base branch/task locations/preview policy | Versioned project profile |
| Credentials and machine paths | Private environment file |

No secret, raw provider reasoning, or provider authentication material belongs
in Git, a PR, or a Discord message.
