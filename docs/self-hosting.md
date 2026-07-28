# Self-hosting: can Ariadne develop and deploy Ariadne?

Deferred design note.  Nothing here is implemented.  It records what the
architecture already permits, what it does not, and the order in which the
remaining pieces would have to be built.

The question splits into two halves with very different risk.

## Half A: Ariadne develops Ariadne

This is close to pure configuration.  A self-hosting profile would point
`ARIADNE_PROJECT_REPO` at the Ariadne checkout, with worktrees under a separate
worktree root.  Every session then gets an isolated worktree of the Ariadne
source; the running service checkout is not a write target, exactly as for any
other project.  The existing gates apply unchanged: PLAN, owner approval, TASK,
owner approval, one Hermes commit, review, Draft PR, CI, owner `!accept`.

This has effectively been rehearsed once already: a separate checkout of this
repository was used as a dogfood target during the fixture phase.

Two prerequisites are outstanding.  The service GitHub token is deliberately
scoped to pull-request operations on the target project; self-hosting requires
it to reach this repository as well.  And the service checkout has to sit on a
clean, pushed branch, because `doctor` requires a clean service checkout.

## Half B: Ariadne installs and restarts itself

This is the part with real hazards, and the mechanism that makes it tractable
already exists.

Turns are independent `systemd --user` transient units, not children of the bot
process.  Restarting `ariadne.service` therefore cannot kill a running turn: the
service's `KillMode=control-group` governs only its own cgroup.  On the way back
up, `Coordinator.reconcile` sees the unit still active and leaves it alone
rather than marking it interrupted.  A deploy can consequently run *as a turn*
and outlive the restart it triggers, which is the whole requirement - the thing
performing the switch must not be the thing being switched.

A second property falls out of the same arrangement.  If the deploy executor is
a plain script that never imports the package, replacing the installed code
underneath it has no effect on the running deployer: the old, known-good deploy
logic supervises the whole switch, and the new code is first executed only when
that script shells out to `doctor` and to the restarted service.  That ordering
is what makes automatic rollback trustworthy, so the executor should stay a
standalone script rather than a subcommand of the package it deploys.

What would have to be added: a deploy turn kind, an owner-gated trigger in the
same shape as `!accept`, and an executor that records the current commit,
fast-forwards, installs, runs the tests, runs `doctor`, restarts, waits for the
gateway to reconnect, and reinstalls the recorded commit if any step fails.

## The three real problems

**Ariadne could weaken its own gates.**  The safety of the pipeline lives in the
gate modules and the Discord action surface.  Nothing today prevents a TASK
allowlist from naming those files, so one approved change could relax the checks
and then deploy itself.  The only current defence is the owner reading the
review and the Draft PR - which is precisely the effort self-hosting is meant to
save.  `protected_sibling_checkouts` does not help: it protects sibling
checkouts outside the repository, not paths inside it.

The missing mechanism is a profile-level set of paths that no TASK allowlist may
ever include, enforced where the TASK is validated.  Under a self-hosting
profile that set would cover the pipeline package, the section parser, the
deploy directory, and the profiles themselves.  The resulting property is the
honest version of self-deployment: Ariadne may change most of itself
automatically, but is structurally unable to auto-deploy a change to its own
gates.

Note that this narrows, but does not remove, the conflict with the stated
invariant that the source repository is never a model write target.  Adopting a
self-hosting profile is a deliberate amendment to that rule, not an oversight,
and should be recorded as one.

**A bad deploy removes the channel used to repair it.**  `doctor` runs as
`ExecStartPre`, so a failure leaves the unit in a five-second restart loop and
the bot never reaches Discord; the only remaining route in is SSH.  Automatic
rollback is therefore a precondition of self-deployment rather than a
refinement, which is what the surviving-transient-unit design above is for.

**Token scope widens.**  Reaching this repository means the service credential
can write the harness's own source.  `!accept` is owner-only and verifies the
full head SHA, and Draft PR creation is a status-card action, so the path stays
owner-gated - but it is a deliberate change of posture and should be taken
consciously.

## Suggested order

Build the deploy executor first as an ordinary script driven by CI, and operate
it manually.  It is the same component self-deployment would need, so running it
by hand for a while means the most dangerous code is well exercised before any
turn is allowed to invoke it.  Only then add the self-hosting profile, the
protected-path mechanism, and the deploy turn that wraps the identical script.
