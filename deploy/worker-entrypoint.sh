#!/bin/sh
# Worker entrypoint. Starts as root only to delegate a cgroup v2 subtree to
# the worker user (per-run memory/CPU/process limits), then drops to that
# user for good. Every cgroup step is best effort: without cgroup v2 or
# privileges the worker still starts, with limits off (it logs why).
set -u
CG=/sys/fs/cgroup
USER_NAME=playground

if [ "$(id -u)" = 0 ]; then
  if [ -f "$CG/cgroup.controllers" ] && [ -w "$CG/cgroup.procs" ]; then
    if mkdir -p "$CG/init" "$CG/worker/main" 2>/dev/null \
      && for p in $(cat "$CG/cgroup.procs"); do echo "$p" > "$CG/init/cgroup.procs" 2>/dev/null; done \
      && echo "+memory +pids +cpu" > "$CG/cgroup.subtree_control" \
      && echo $$ > "$CG/worker/main/cgroup.procs" \
      && echo "+memory +pids +cpu" > "$CG/worker/cgroup.subtree_control" \
      && chown "$USER_NAME" "$CG/worker" "$CG/worker/cgroup.procs" \
           "$CG/worker/cgroup.subtree_control" "$CG/worker/main" "$CG/worker/main/cgroup.procs"
    then
      export RUN_CGROUP_ROOT="$CG/worker"
    else
      echo "worker-entrypoint: could not delegate cgroup v2; per-run limits stay off" >&2
    fi
  fi
  mkdir -p /run/playground && chown "$USER_NAME" /run/playground && chmod 711 /run/playground
  exec setpriv --reuid="$USER_NAME" --regid="$USER_NAME" --init-groups \
    --inh-caps=-all --bounding-set=-all --no-new-privs -- "$@"
fi
exec "$@"
