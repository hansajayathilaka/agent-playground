import { create, type MessageInitShape } from "@bufbuild/protobuf";
import { describe, expect, it } from "vitest";
import { type Event, EventSchema } from "../gen/platform/v1/event_pb.js";
import { parseRoute, routeHash } from "./route.js";
import { applyBatch, childrenOf, emptyTranscript } from "./transcript.js";

let offset = 0n;
type Init = MessageInitShape<typeof EventSchema>;

// Test helper: oneof bodies are given as plain init objects.
function ev(body: { case: string; value: object }, parent = "", runId = "r1"): Event {
  offset += 1n;
  return create(EventSchema, { offset, runId, parentToolUseId: parent, body } as Init);
}

describe("transcript", () => {
  it("streams deltas, replaces them with final text and nests subagents", () => {
    offset = 0n;
    const events = [
      ev({ case: "userMessage", value: { text: "hi" } }),
      ev({ case: "runStatus", value: { status: "running", error: "" } }),
      ev({ case: "textDelta", value: { messageId: "m1", index: 0, text: "Hel" } }),
      ev({ case: "textDelta", value: { messageId: "m1", index: 0, text: "lo" } }),
      ev({ case: "toolCall", value: { toolUseId: "t1", name: "Task", inputJson: "{}" } }),
      ev({ case: "assistantText", value: { messageId: "m2", index: 0, text: "sub" } }, "t1"),
      ev({ case: "assistantText", value: { messageId: "m1", index: 0, text: "Hello!" } }),
      ev({ case: "toolResult", value: { toolUseId: "t1", content: "ok", isError: false } }),
      ev({ case: "runStatus", value: { status: "succeeded", error: "" } }),
    ];
    const t = applyBatch(emptyTranscript, events);
    expect(t.lastOffset).toBe(9n);
    const [turn] = t.turns;
    expect(turn?.prompt).toBe("hi");
    expect(turn?.status).toBe("succeeded");
    if (!turn) throw new Error("no turn");
    const top = childrenOf(turn, "");
    expect(top.map((i) => i.kind)).toEqual(["text", "tool"]);
    expect(top[0]).toMatchObject({ text: "Hello!", final: true });
    expect(top[1]).toMatchObject({ name: "Task", result: "ok" });
    expect(childrenOf(turn, "t1")).toMatchObject([{ text: "sub" }]);
  });

  it("ignores duplicates and heartbeats, and replay equals live", () => {
    offset = 0n;
    const events = [
      ev({ case: "userMessage", value: { text: "a" } }),
      ev({ case: "textDelta", value: { messageId: "m", index: 0, text: "x" } }),
      ev({ case: "userMessage", value: { text: "b" } }, "", "r2"),
    ];
    const live = events.reduce((t, e) => applyBatch(t, [e]), emptyTranscript);
    const replay = applyBatch(emptyTranscript, events);
    expect(replay).toEqual(live);
    const again = applyBatch(replay, [
      ...events,
      create(EventSchema, { body: { case: "heartbeat", value: {} } }),
    ]);
    expect(again).toBe(replay);
    expect(replay.turns.map((t) => t.prompt)).toEqual(["a", "b"]);
  });
});

describe("routes", () => {
  it("round-trips", () => {
    for (const hash of [
      "#/credentials",
      "#/agents/a/files",
      "#/agents/a/chat",
      "#/agents/a/chat/c-1",
    ]) {
      expect(routeHash(parseRoute(hash))).toBe(hash);
    }
    expect(parseRoute("#/agents/a")).toMatchObject({ page: "agent", view: "files" });
    expect(parseRoute("")).toEqual({ page: "home" });
  });
});
