# LuxrayKit cutover and rollback

This procedure intentionally does **not** switch the VPS until the private
fixture dogfood in [../dogfood.md](../dogfood.md) passes.  LuxrayKit stays
untouched during the extraction.  The active r9 release and old user service
are the rollback point.

## Completed cutover record

The fixture passed before the controlled switch.  The VPS now runs
`ariadne.service` from Ariadne revision `ea05767` with a dedicated virtual
environment and private configuration.  The service passed doctor, connected
to the Discord Gateway, and migrated the retained state database from schema
v5 to v9 after a private SQLite backup was created.

The old `dev-pipeline-harness.service` unit file and r9 release remain in place
for rollback, but the unit is disabled so it cannot start alongside Ariadne on
the next user-manager restart.  The temporary fixture service is stopped, and
its disposable deploy key/token material has been revoked.

No LuxrayKit task, Draft PR, preview check, Wrangler action, or merge was run
as part of this cutover.  The planned LuxrayKit docs-only dogfood is explicitly
deferred by the owner.

## Target layout

```text
/home/ubuntu/Ariadne/                         # program source, never model-written
/home/ubuntu/LuxrayKit/                       # target project checkout, never model-written
/home/ubuntu/LuxrayKit-dev-worktrees/S-####/  # only model write targets

/home/ubuntu/.config/ariadne/env              # private 0600 config
/home/ubuntu/.local/share/ariadne/venv/       # Ariadne runtime
/home/ubuntu/.local/share/dev-pipeline-harness/harness.sqlite3
                                               # retained r9 state during first cutover
```

The first cutover deliberately keeps the existing SQLite state directory and
worktree root.  Ariadne migrates schema v5 to v9 in place, adding durable TASK
fields, executor type, and a persisted post-turn phase.  Do not cut over with
an active legacy turn.

## Preflight

1. On the VPS, verify the legacy service is active or cleanly idle and inspect
   only the exact active unit names.  If an old turn is active, let it finish
   or explicitly stop that exact unit and reconcile it before proceeding.
2. Create an SQLite backup using `.backup` in a private 0700 backup directory.
   Preserve the old r9 release directory and its unit file; do not delete it.
3. Clone/pull Ariadne to `/home/ubuntu/Ariadne` on the reviewed Ariadne commit.
   Do not reuse `/home/ubuntu/LuxrayKit` as the Ariadne source checkout.
4. Create a separate virtual environment under
   `/home/ubuntu/.local/share/ariadne/venv` and install the reviewed Ariadne
   source there.  Keep provider login state where the provider CLIs already
   expect it; never copy authentication files into Ariadne.
5. Create `/home/ubuntu/.config/ariadne/env` with mode `0600` inside a mode
   `0700` directory.  The essential non-secret migration values are:

   ```dotenv
   ARIADNE_PROFILE=/home/ubuntu/Ariadne/profiles/luxraykit.toml
   ARIADNE_PROJECT_REPO=/home/ubuntu/LuxrayKit
   ARIADNE_WORKTREE_ROOT=/home/ubuntu/LuxrayKit-dev-worktrees
   ARIADNE_STATE_DIR=/home/ubuntu/.local/share/dev-pipeline-harness
   HERMES_BIN=/home/ubuntu/.local/bin/hermes
   ```

   Carry over Discord IDs/token and explicit provider executable/model/effort
   allowlists privately.  Do not paste them in shell history, Git, or chat.
6. Run `python -m ariadne doctor --env-file <private-env>` from the new venv.
   It must pass before the new service is enabled.  It checks the profile,
   paths, worktree isolation, Hermes/Codex/Claude executables, GitHub login,
   SQLite migration, and the one-runner invariant.

## Controlled service switch

Install [../../deploy/systemd/ariadne.service](../../deploy/systemd/ariadne.service)
as the user unit `ariadne.service`, after reviewing the source checkout, venv,
and env-file paths for the actual VPS.  Stop the old
`dev-pipeline-harness.service` only after the new unit has passed `doctor`.
Start Ariadne, verify its `systemd --user` status, Discord gateway connection,
and the existing state summary.  Do not delete or overwrite the old unit.

The fixture dogfood was the first live acceptance and passed.  The next phase
is a LuxrayKit profile task that changes only a documentation file.  Its Draft
PR must pass CI and the existing read-only preview check; do not use Wrangler
as part of this extraction.  That phase is currently deferred.  A merge remains
an explicit `!accept` decision by the owner and is outside the cutover itself.

## Rollback

If Ariadne doctor, gateway setup, reconciliation, or the fixture dogfood
fails, stop `ariadne.service` and re-enable the preserved legacy service using
the unchanged r9 release, old private env, same SQLite file, and same
worktrees.  Do not reset, delete, or recreate any `S-####` worktree.  Inspect
the backed-up SQLite state and the exact result/transcript files before another
attempt.
