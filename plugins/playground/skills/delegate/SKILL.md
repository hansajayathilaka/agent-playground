---
name: delegate
description: Give a task to one of the workspace's agents and run it in that agent's own folder in the background (claude -p on the user's own login), then check progress and collect the result. Use when the user asks an agent to do something, wants work done in parallel, or asks about a running or finished task.
argument-hint: "<agent> <task> | status | result <run-id>"
---

# Delegate a task to an agent

Request: $ARGUMENTS

The helper CLI is `python "${CLAUDE_SKILL_DIR}/../../scripts/pg.py"` (`pg` below).

## Start a run

1. `pg list` if you're unsure which agent fits. Pick the one whose description matches; if none does, offer to build one with the `build-agent` skill.
2. Write the task as a self-contained brief: the agent starts with only its own folder and memory, not this conversation. Include file paths (absolute, or relative to the agent folder), what to produce and where.
3. `pg run <agent> "<brief>" --background`. It prints the run id at once and keeps running after this session ends. Use the foreground form (no `--background`) only for short tasks where the user is waiting.
4. Tell the user the run id and that they can follow it in the Playground pane (`/playground`), or ask you for status.

Several runs on different agents can go in parallel. Avoid two runs on the same agent at once when they write the same files.

## Follow up

- `pg runs` lists recent runs with status (`queued`, `running`, `done`, `failed`, `lost` when the process vanished) and the current activity.
- `pg show <run-id>` prints the run record and the final result. Full event logs are in `.playground/runs/<run-id>/events.jsonl`.
- To continue a finished run's conversation: `pg run <agent> "<follow-up>" --background --resume <run-id>`.
- Failed runs: read `stderr.log` and the last events, explain the cause, and suggest the fix (usually a permission the agent's settings don't allow, a missing tool, or a connector that isn't enabled).
