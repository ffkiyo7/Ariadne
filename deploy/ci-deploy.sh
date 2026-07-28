#!/usr/bin/env bash
#
# VPS-side deploy endpoint for Ariadne.
#
# This runs as the forced command of a restricted SSH key, so the only thing a
# caller controls is SSH_ORIGINAL_COMMAND, which must name a commit already
# reachable from the deploy branch on the remote.  It is deliberately installed
# OUTSIDE the checkout it deploys (see docs/deploy.md): replacing the source
# tree must not swap the deployer out from under a running deploy, and this
# script never imports the package, so the code performing the switch is always
# the code that was verified before it.
#
# Rollback is a precondition, not a refinement.  `doctor` runs as ExecStartPre,
# so a bad install leaves the unit in a restart loop with no Discord left to
# repair it from.
set -Eeuo pipefail

REPO="${ARIADNE_REPO:-$HOME/Ariadne}"
VENV="${ARIADNE_VENV:-$HOME/.local/share/ariadne/venv}"
ENV_FILE="${ARIADNE_ENV_FILE:-$HOME/.config/ariadne/env}"
BRANCH="${ARIADNE_DEPLOY_BRANCH:-main}"
UNIT="ariadne.service"
LOG_DIR="$HOME/.local/share/ariadne"
LOG="$LOG_DIR/deploy.log"
LOCK="$LOG_DIR/deploy.lock"
GATEWAY_TIMEOUT=90

# systemctl --user needs these, and a forced command is not guaranteed to be a
# full login shell.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=${XDG_RUNTIME_DIR}/bus}"

mkdir -p "$LOG_DIR"

log() { printf '%s  %s\n' "$(date -Is)" "$*" | tee -a "$LOG"; }
die() { log "REFUSED: $*"; exit 64; }

PREVIOUS=""
ROLLED_BACK=0

rollback() {
    local previous="$1"
    log "ROLLBACK -> ${previous:0:12}"
    git -C "$REPO" reset --hard --quiet "$previous"
    "$VENV/bin/pip" install --quiet . >>"$LOG" 2>&1
    systemctl --user restart "$UNIT"
    if wait_healthy "$(date '+%Y-%m-%d %H:%M:%S')"; then
        log "ROLLBACK OK: running ${previous:0:12}"
    else
        log "ROLLBACK UNHEALTHY: ${previous:0:12} did not reach the gateway - manual repair needed"
        return 1
    fi
}

on_error() {
    local code=$?
    trap - ERR EXIT
    if [[ -n "$PREVIOUS" && "$ROLLED_BACK" -eq 0 ]]; then
        ROLLED_BACK=1
        rollback "$PREVIOUS" || true
    fi
    log "DEPLOY FAILED (exit $code)"
    exit "$code"
}
trap on_error ERR

wait_healthy() {
    local since="$1" deadline=$((SECONDS + GATEWAY_TIMEOUT))
    while ((SECONDS < deadline)); do
        if systemctl --user is-active --quiet "$UNIT" &&
            journalctl --user -u "$UNIT" --since "$since" --no-pager 2>/dev/null |
                grep -q "has connected to Gateway"; then
            return 0
        fi
        sleep 3
    done
    return 1
}

target="${SSH_ORIGINAL_COMMAND:-${1:-}}"
[[ "$target" =~ ^[0-9a-f]{40}$ ]] || die "expected a 40-character lowercase commit SHA"

exec 9>"$LOCK"
flock -n 9 || die "another deploy is in progress"

cd "$REPO"

# The cutover rule: never switch code under an executing turn.  Turns are their
# own transient units, so a restart would not kill one, but it would swap the
# code its completion gates are about to run.
active_turns="$(systemctl --user list-units --type=service --state=active --no-legend \
    'ariadne-turn-*' 'dev-pipeline-turn-*' 2>/dev/null || true)"
[[ -z "$active_turns" ]] || die "a turn is active: $(awk '{print $1}' <<<"$active_turns" | paste -sd, -)"

current_branch="$(git rev-parse --abbrev-ref HEAD)"
[[ "$current_branch" == "$BRANCH" ]] ||
    die "service checkout is on '$current_branch', not the deploy branch '$BRANCH'"
[[ -z "$(git status --porcelain)" ]] || die "service checkout is not clean"

PREVIOUS="$(git rev-parse HEAD)"
if [[ "$PREVIOUS" == "$target" ]]; then
    log "already at ${target:0:12}; nothing to deploy"
    exit 0
fi

log "deploy ${PREVIOUS:0:12} -> ${target:0:12}"

git fetch --quiet origin
# An unknown commit makes merge-base fail loudly; the refusal below is the
# useful message, so keep git's own complaint out of the CI log.
git merge-base --is-ancestor "$target" "origin/$BRANCH" 2>/dev/null ||
    die "${target:0:12} is not reachable from origin/$BRANCH"
git merge-base --is-ancestor "$PREVIOUS" "$target" 2>/dev/null ||
    die "${target:0:12} is not a fast-forward from ${PREVIOUS:0:12}; revert on $BRANCH instead"

git merge --ff-only --quiet "$target"
log "checkout updated"

"$VENV/bin/pip" install --quiet . >>"$LOG" 2>&1
log "installed into the venv"

PYTHONPATH=src "$VENV/bin/python" -m unittest discover -s tests -p 'test_*.py' -t tests >>"$LOG" 2>&1
log "tests passed"

"$VENV/bin/python" -m ariadne doctor --env-file "$ENV_FILE" >>"$LOG" 2>&1
log "doctor passed"

restart_at="$(date '+%Y-%m-%d %H:%M:%S')"
systemctl --user restart "$UNIT"
wait_healthy "$restart_at" || {
    log "service did not reach the Discord gateway within ${GATEWAY_TIMEOUT}s"
    false
}

log "DEPLOYED ${target:0:12}"

# The endpoint is installed outside the checkout on purpose, so it does not
# update itself.  Say so when the just-deployed commit changes it.
if ! cmp -s "$REPO/deploy/ci-deploy.sh" "$0"; then
    log "NOTE: $REPO/deploy/ci-deploy.sh differs from the installed endpoint at $0 - reinstall it by hand"
fi
