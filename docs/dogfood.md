# Fixture dogfood procedure

The first real closed loop must run against a private fixture repository, not
LuxrayKit.  Its task should change only one Markdown file and must not deploy,
merge, touch a production credential, or invoke Wrangler.

## Completed fixture run

The fixture acceptance passed with durable state and live GitHub facts aligned:

- Session `S-0001` completed the Discord → PLAN/TASK → Hermes → strong-model
  review → owner confirmation → Draft PR → CI sequence.
- The reviewed head was `60e0831d295461eec168a2d4319ad301360568c3` on
  `ariadne/S-0001`.  The private fixture's Draft PR #1 contains only the
  intended `README.md` change and remains open and unmerged.
- Its `Fixture CI` `docs` check completed successfully; Ariadne persisted the
  session as `ci_passed`.  The fixture profile has no preview gate.
- A fixture-only Codex sandbox network exception was used to work around the
  host sandbox startup failure.  It was not carried into the normal Ariadne
  service, where `CODEX_WORKSPACE_NETWORK_ACCESS=false`.
- The temporary fixture deploy key, VPS token copy, and worktree SSH override
  were revoked after the result was recorded.  The Draft PR and its durable
  audit evidence are retained.

This is acceptance of the reusable engine, not authorization to merge the
fixture PR or a claim that LuxrayKit has completed its separate dogfood.

## Preconditions

- Ariadne's unit tests, source compilation, and `git diff --check` pass.
- The VPS doctor passes with a private fixture profile and no active Ariadne
  or legacy transient turn.
- The fixture is a private GitHub repository with a `main` branch and CI that
  can pass for a documentation-only change.
- The owner has reviewed the configured Discord Thread, model/effort card,
  `HERMES_BIN`, and target checkout paths.

If Codex reports a sandbox startup failure involving `bwrap` loopback or
`RTM_NEWADDR`, do not switch it to full access.  On a reviewed, isolated
fixture-only runtime, the owner may set `CODEX_WORKSPACE_NETWORK_ACCESS=true`
in the private environment and restart that temporary service.  This preserves
the worktree-write filesystem boundary but permits model-issued network access;
record that exception in the fixture audit and leave the option disabled for
normal deployments unless their egress policy has been reviewed.

## One intended run

1. Use `/dispatch` and open the configured Thread.
2. Let the chosen strong model write a PLAN and approve it.
3. Let it write a strict TASK that allows only the fixture Markdown file and
   includes a deterministic verification command.
4. Use the status-card TASK action.  Confirm the transient Hermes turn, its
   terminal result, the locked verification audit (including one local,
   allowed-file-only commit), and `review_pending`.
5. Use the review action, read the strong-model review transcript, and confirm
   it on the status card.
6. Create a Draft PR, then record green CI.  Stop here: no fixture merge is
   required for this acceptance.

## Pass/fail evidence

Pass requires the Discord Thread to remain usable after each state change; the
SQLite session/turn/audit trail to match the Thread; a real worktree branch;
and one Draft PR whose head SHA is the reviewed branch head.  A failed test,
scope check, stopped unit, or missing result must produce an owner-visible
recoverable state rather than silently proceeding.

The VPS service source was switched to Ariadne after this pass.  The subsequent
LuxrayKit dogfood remains a separate docs-only Draft PR with CI plus a
read-only preview check; it is intentionally deferred at the owner's request.
`!accept` remains a later owner decision.
