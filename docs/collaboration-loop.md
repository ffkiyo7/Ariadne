# The collaboration loop

Ariadne's worker tier is only a collaboration - rather than a remote
subprocess with extra steps - if the worker can push back, the owner can
accept on narrative, failure feeds the next attempt, and knowledge outlives
the task.  This document records those four mechanisms and their design
rationale.

## 1. NEEDS_CLARIFICATION

A Hermes turn has exactly two terminal outcomes:

- **IMPLEMENT**: one local commit containing only allowed changes, or
- **CLARIFY**: a single `ARIADNE-CLARIFICATION.md` at the worktree root with
  five mandatory sections (`Blocker`, `Why the TASK is insufficient`,
  `Options`, `Recommendation`, `Why / Impact`), no other change, no commit.

The runner's completion gate detects the file before normal TASK
verification, records it durably, moves the raw file into the private
session directory, and ends the turn with a `NEEDS_CLARIFICATION` summary.
The session lands in `needs_owner`; the bot posts a question card in the
Thread and the pinned status card links to it.  The owner answers by
revising the TASK and re-approving - there is deliberately **no free-form
model-to-model dialogue** and no unbounded question rounds.

### Why a compliance-biased worker will still use it

A model that prefers to be agreeable will not volunteer doubt, so the
protocol avoids relying on volunteered doubt:

- **Forced binary choice.**  The prompt frames IMPLEMENT/CLARIFY as two
  mutually exclusive terminal outcomes decided *before* touching any file.
  Not choosing is itself a protocol violation.
- **Objective tripwires.**  Clarification is *required* - phrased as rules,
  not judgment - when the TASK references something that does not exist,
  a needed change falls outside the allowlist, sections contradict, a
  verification command already fails on the untouched baseline, the DoD is
  unverifiable, or materially different interpretations exist.  Rule-
  following leans on the compliance bias instead of fighting it.
- **Inverted incentive.**  The prompt states that a justified CLARIFY is a
  success and a plausible implementation of the wrong interpretation is a
  failure ("do not guess in order to appear agreeable").
- **Costly asking.**  A clarification without a filled `Recommendation`
  section is rejected by the parser, so the channel cannot degrade into
  cheap "I'm confused" pings; the worker must finish its own analysis first.
- **Post-hoc audit.**  The review prompt includes a mandatory `TASK 充分性`
  section: the reviewer states whether the implementer should have asked.
  This produces a measurable signal for tuning the tripwires over time.

## 2. Decision pack

When a review turn completes, the bot posts one decision-pack embed whose
**primary content is model narrative**: the implementer's commit-message
self-report (subject, reasoning, own risk judgment) and the reviewer's
structured conclusion (`结论`, `改动核对`, `行为变化`, `风险与遗留`,
`TASK 充分性`).

Deterministic facts are attached as a *footnote and lie detector*, not as
the main content:

- The commit message must end with a literal `Files:` list; it is
  cross-checked against `git diff --name-status`.  Claimed-but-unchanged and
  changed-but-unclaimed paths are rendered as red mismatch flags, and any
  mismatch turns the embed red.
- diffstat, verification pass counts, and the knowledge-file flag appear in
  one compact facts line.
- Full facts (file list, verification commands, the complete head SHA used
  by `!accept`) sit behind an "事实详情" button that replies ephemerally.

Discord has no true collapse/fold primitive; the ephemeral detail button is
the fold equivalent, and the pinned status card carries jump links to the
newest question card and decision pack so the owner never scrolls the
Thread hunting for the decision surface.

## 3. Retry context

Without feedback there is no loop, only repeated invocation.  When a TASK is
re-approved after a failure, the next Hermes prompt automatically gains one
bounded "previous round evidence" section containing, in order: the previous
terminal error, the worker's own clarification (when the revision answers
it), failed verification commands with their bounded output, and the
owner's revision feedback.

Boundaries, chosen deliberately:

- Only evidence newer than the previous TASK approval is included, so round
  three is not haunted by round-one noise that was already fixed.
- Raw reviewer prose is excluded; the owner's "打回并修订" feedback and the
  revised TASK are the curated channel for review conclusions.
- Hard caps: ~700 characters per item, ~3000 total.

The loop is therefore: fail → evidence extracted → owner revises explicitly
→ retry with the evidence in-prompt.  The "打回并修订 TASK…" action on the
status card closes the previously missing path from a completed review back
to a revised TASK.

## 4. Membership: the shared knowledge file

If every task runs in a fresh worktree with a frozen prompt, the worker
learns nothing across tasks - isolation would silently defeat membership.
The profile's optional `knowledge_file` is the counterweight:

- It is silently added to every TASK allowlist, so the worker may append
  findings **in the same reviewed commit** without the TASK author having to
  remember it.
- The prompt instructs the worker to read `AGENTS.md` and the knowledge file
  before implementing, and to append short, dated, factual entries (≤10
  lines, never rewriting existing entries, no speculation).
- Knowledge accumulates in the repository: versioned, reviewed by the strong
  model, inherited by any future worker.
- `AGENTS.md` remains owner-curated.  Promoting journal entries into
  standing guidance is an explicit owner/strong-model act, not something the
  worker does unilaterally.

The decision pack marks commits that include knowledge updates so the owner
sees membership working (or not) at acceptance time.
