#!/bin/sh
# Worker entrypoint. Starts as root only to delegate a cgroup v2 subtree to
# the worker user (per-run memory/CPU/process limits), then drops to that
# user for good. Every cgroup step is best effort: without cgroup v2 or
# privileges the worker still starts, with limits off (it logs why).
set -u
CG=/sys/fs/cgroup
USER_NAME=playground
CONTROLLERS="+memory +pids +cpu"

fail() {
  echo "worker-entrypoint: cannot delegate cgroup v2 ($1); per-run limits stay off" >&2
  return 1
}

delegate_cgroups() {
  [ -f "$CG/cgroup.controllers" ] || { fail "no cgroup v2 at $CG"; return 1; }
  mkdir -p "$CG/init" "$CG/worker/main" 2>/dev/null || { fail "read-only $CG"; return 1; }
  # A cgroup with enabled controllers can't hold processes, so move every
  # process out of the namespace root first. Pids that exit meanwhile
  # (including the reader below) fail harmlessly.
  for p in $(cat "$CG/cgroup.procs"); do
    echo "$p" > "$CG/init/cgroup.procs" 2>/dev/null || true
  done
  echo "$CONTROLLERS" > "$CG/cgroup.subtree_control" || { fail "enabling controllers at root"; return 1; }
  echo $$ > "$CG/worker/main/cgroup.procs" || { fail "moving the worker into worker/main"; return 1; }
  echo "$CONTROLLERS" > "$CG/worker/cgroup.subtree_control" || { fail "enabling controllers in worker/"; return 1; }
  chown "$USER_NAME" "$CG/worker" "$CG/worker/cgroup.procs" "$CG/worker/cgroup.subtree_control" \
    "$CG/worker/main" "$CG/worker/main/cgroup.procs" || { fail "chown"; return 1; }
  return 0
}

if [ "$(id -u)" = 0 ]; then
  if delegate_cgroups; then
    export RUN_CGROUP_ROOT="$CG/worker"
  fi
  mkdir -p /run/playground && chown "$USER_NAME" /run/playground && chmod 711 /run/playground
  exec setpriv --reuid="$USER_NAME" --regid="$USER_NAME" --init-groups \
    --inh-caps=-all --bounding-set=-all --no-new-privs -- "$@"
fi
exec "$@"
