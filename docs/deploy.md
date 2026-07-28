# Deployment

Ariadne deploys from GitHub Actions.  Pushing to `main` runs the test suite and,
only if it passes, hands the commit to a restricted endpoint on the VPS which
installs it, verifies it, and reinstalls the previous commit if verification
fails.

```text
push main -> Actions: test -> Actions: deploy -> ssh (forced command)
                                                   -> ff-only to the commit
                                                   -> pip install into the venv
                                                   -> tests + doctor
                                                   -> restart + gateway check
                                                   -> rollback on any failure
```

Branches and pull requests run the tests only.  The deploy job is gated on
`github.event_name == 'push' && github.ref == 'refs/heads/main'` because this
repository is public: the credential must never be reachable from code that has
not landed on the deploy branch.

## The deploy credential

The workflow authenticates with an SSH key dedicated to deployment.  It is not
the instance's login key, and it does not grant a shell.  Its entry in
`~/.ssh/authorized_keys` pins a forced command and disables everything else:

```text
command="/home/ubuntu/.local/bin/ariadne-ci-deploy",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding ssh-ed25519 AAAA... ariadne-ci-deploy
```

Whatever the client asks to run arrives as `SSH_ORIGINAL_COMMAND` and is
accepted only if it is a 40-character commit SHA that is already reachable from
`origin/main`.  A leaked workflow secret therefore cannot open a shell, and
cannot install a commit that is not on the deploy branch.

Three repository secrets are required:

| Secret            | Contents                                            |
| ----------------- | --------------------------------------------------- |
| `VPS_SSH_KEY`     | Private half of the dedicated deploy key             |
| `VPS_HOST`        | `user@host` for the VPS                              |
| `VPS_KNOWN_HOSTS` | `ssh-keyscan` output, so the runner pins the host key |

The deploy job also runs in the `production` environment, so a required reviewer
can be added there later to make every deploy an explicit approval without
touching the workflow.

## The endpoint is installed outside the checkout

The canonical script lives at [../deploy/ci-deploy.sh](../deploy/ci-deploy.sh),
but the forced command points at a copy at `~/.local/bin/ariadne-ci-deploy`.
This is deliberate.  The deployer must not be swapped out from under itself
while it is replacing the source tree, and it never imports the package, so the
logic supervising a switch is always logic that was verified before that switch
began.  The same property is what makes the rollback trustworthy.

The cost is that the endpoint does not update itself.  When a deployed commit
changes `deploy/ci-deploy.sh`, the deploy log says so, and the copy has to be
reinstalled by hand:

```bash
install -m 700 ~/Ariadne/deploy/ci-deploy.sh ~/.local/bin/ariadne-ci-deploy
```

## What the endpoint refuses

* A caller-supplied string that is not a 40-character lowercase SHA.
* A commit that is not reachable from `origin/main`.
* A commit that is not a fast-forward from what is installed.  Roll back by
  reverting on `main`, not by deploying an older commit.
* A service checkout that is on some other branch, or is dirty.
* Any active `ariadne-turn-*` unit.  A restart would not kill a turn - turns are
  their own transient units - but it would swap the code its completion gates
  are about to run.
* A concurrent deploy, via `flock`.

## Verification and rollback

After installing, the endpoint runs the full test suite and `ariadne doctor`
from the venv, restarts the unit, and then waits up to 90 seconds for the
Discord gateway to reconnect in the journal.  Any failure from the install
onward reinstalls the recorded previous commit and restarts again.

Rollback is a precondition rather than a refinement: `doctor` runs as
`ExecStartPre`, so a bad install leaves the unit in a five-second restart loop
and there is no Discord left to repair it from.  If rollback itself fails to
come back healthy, the log says so explicitly and the only route in is SSH.

The full transcript of every deploy, including pip and test output, is appended
to `~/.local/share/ariadne/deploy.log` on the VPS.

## Prerequisite: the service checkout tracks `main`

The endpoint refuses to deploy onto a checkout that is not on the deploy branch.
The service checkout therefore has to be on `main`, and `main` has to be pushed,
before any of this works.

## Related

Whether this pipeline should eventually be driven by Ariadne itself rather than
by GitHub Actions is discussed in [self-hosting.md](self-hosting.md).  The
endpoint here is the component that design would reuse, which is why it is worth
operating it from CI first.
