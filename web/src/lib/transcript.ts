// Folds the platform event stream of one conversation into a transcript the
// chat view renders. Pure, so it is unit-tested and replay after a page
// refresh gives exactly the live result.
import type { Event } from "../gen/platform/v1/event_pb.js";

export type RunState = "queued" | "running" | "succeeded" | "failed" | "cancelled" | "interrupted";

export interface TextItem {
  kind: "text" | "thinking";
  key: string;
  parent: string;
  text: string;
  final: boolean;
}

export interface ToolItem {
  kind: "tool";
  key: string;
  parent: string;
  toolUseId: string;
  name: string;
  input: string;
  result?: string;
  isError: boolean;
  subagent?: { description: string; status: string; summary: string };
}

export interface NoticeItem {
  kind: "notice";
  key: string;
  parent: string;
  level: string;
  message: string;
}

export type Item = TextItem | ToolItem | NoticeItem;

export interface Turn {
  runId: string;
  prompt: string;
  status: RunState;
  error: string;
  items: Item[];
  costUsd?: number;
  numTurns?: number;
  durationMs?: number;
}

export interface Transcript {
  turns: Turn[];
  lastOffset: bigint;
}

export const emptyTranscript: Transcript = { turns: [], lastOffset: 0n };

export function isActive(status: RunState): boolean {
  return status === "queued" || status === "running";
}

function turnFor(turns: Turn[], runId: string): Turn {
  let turn = turns.find((t) => t.runId === runId);
  if (!turn) {
    turn = { runId, prompt: "", status: "queued", error: "", items: [] };
    turns.push(turn);
  }
  return turn;
}

function upsertText(
  turn: Turn,
  kind: "text" | "thinking",
  parent: string,
  messageId: string,
  index: number,
  text: string,
  final: boolean,
): void {
  const key = `${kind}|${parent}|${messageId}|${index}`;
  const existing = turn.items.find((i): i is TextItem => i.key === key);
  if (!existing) {
    turn.items.push({ kind, key, parent, text, final });
  } else if (final) {
    existing.text = text;
    existing.final = true;
  } else if (!existing.final) {
    existing.text += text;
  }
}

function toolFor(turn: Turn, toolUseId: string, parent: string): ToolItem {
  let tool = turn.items.find((i): i is ToolItem => i.kind === "tool" && i.toolUseId === toolUseId);
  if (!tool) {
    tool = {
      kind: "tool",
      key: `tool|${toolUseId}`,
      parent,
      toolUseId,
      name: "",
      input: "",
      isError: false,
    };
    turn.items.push(tool);
  }
  return tool;
}

/** Apply a batch of events. Returns a new transcript; the input is not
 * modified (it is copied once per batch, so replaying thousands of streamed
 * deltas stays cheap). */
export function applyBatch(t: Transcript, events: Event[]): Transcript {
  const fresh = events.filter((ev) => ev.offset > t.lastOffset && ev.body.case !== "heartbeat");
  if (!fresh.length) return t;
  const turns = t.turns.map((x) => ({ ...x, items: x.items.map((i) => ({ ...i })) }));
  let last = t.lastOffset;
  for (const ev of fresh) {
    if (ev.offset <= last) continue; // duplicate after a reconnect
    applyOne(turns, ev);
    last = ev.offset;
  }
  return { turns, lastOffset: last };
}

export function apply(t: Transcript, ev: Event): Transcript {
  return applyBatch(t, [ev]);
}

function applyOne(turns: Turn[], ev: Event): void {
  const turn = turnFor(turns, ev.runId);
  const parent = ev.parentToolUseId;
  const body = ev.body;
  switch (body.case) {
    case "userMessage":
      turn.prompt = body.value.text;
      break;
    case "runStatus":
      turn.status = body.value.status as RunState;
      turn.error = body.value.error;
      break;
    case "textDelta":
      upsertText(
        turn,
        "text",
        parent,
        body.value.messageId,
        body.value.index,
        body.value.text,
        false,
      );
      break;
    case "assistantText":
      upsertText(
        turn,
        "text",
        parent,
        body.value.messageId,
        body.value.index,
        body.value.text,
        true,
      );
      break;
    case "thinking":
      upsertText(
        turn,
        "thinking",
        parent,
        body.value.messageId,
        body.value.index,
        body.value.text,
        true,
      );
      break;
    case "toolCall": {
      const tool = toolFor(turn, body.value.toolUseId, parent);
      tool.name = body.value.name;
      tool.input = body.value.inputJson;
      break;
    }
    case "toolResult": {
      const tool = toolFor(turn, body.value.toolUseId, parent);
      tool.result = body.value.content;
      tool.isError = body.value.isError;
      break;
    }
    case "subagent": {
      const tool = toolFor(turn, body.value.toolUseId, parent);
      const prev = tool.subagent;
      tool.subagent = {
        description: body.value.description || prev?.description || "",
        status: body.value.status,
        summary: body.value.summary || prev?.summary || "",
      };
      break;
    }
    case "result":
      turn.costUsd = body.value.costUsd;
      turn.numTurns = body.value.numTurns;
      turn.durationMs = Number(body.value.durationMs);
      break;
    case "notice":
      turn.items.push({
        kind: "notice",
        key: `notice|${ev.offset}`,
        parent,
        level: body.value.level,
        message: body.value.message,
      });
      break;
    default:
      break;
  }
}

/** Items directly under ``parent`` ("" = the top level). Items whose parent
 * tool call is unknown are shown at the top level rather than lost. */
export function childrenOf(turn: Turn, parent: string): Item[] {
  if (parent === "") {
    const tools = new Set(
      turn.items.filter((i): i is ToolItem => i.kind === "tool").map((i) => i.toolUseId),
    );
    return turn.items.filter((i) => i.parent === "" || !tools.has(i.parent));
  }
  return turn.items.filter((i) => i.parent === parent);
}
