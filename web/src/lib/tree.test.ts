import { create } from "@bufbuild/protobuf";
import { describe, expect, it } from "vitest";
import { FileEntry_Kind, FileEntrySchema } from "../gen/platform/v1/file_pb.js";
import { buildTree, decodeText, joinPath, languageFor, targetDir } from "./tree.js";

const f = (path: string) => create(FileEntrySchema, { path, kind: FileEntry_Kind.FILE });
const d = (path: string) => create(FileEntrySchema, { path, kind: FileEntry_Kind.DIRECTORY });

describe("buildTree", () => {
  it("nests entries and sorts folders first", () => {
    const tree = buildTree([
      d(".claude"),
      d(".claude/rules"),
      f(".claude/rules/a.md"),
      f(".claude/settings.json"),
      f("CLAUDE.md"),
      f("agent.json"),
      d("memory"),
    ]);
    expect(tree.map((n) => n.name)).toEqual([".claude", "memory", "agent.json", "CLAUDE.md"]);
    expect(tree[0]?.children.map((n) => n.path)).toEqual([
      ".claude/rules",
      ".claude/settings.json",
    ]);
    expect(tree[0]?.children[0]?.children[0]?.path).toBe(".claude/rules/a.md");
  });
});

describe("helpers", () => {
  it("picks the folder for new files", () => {
    expect(targetDir(null, false)).toBe("");
    expect(targetDir("memory", true)).toBe("memory");
    expect(targetDir("memory/MEMORY.md", false)).toBe("memory");
    expect(targetDir("CLAUDE.md", false)).toBe("");
    expect(joinPath("memory", "/x.md")).toBe("memory/x.md");
    expect(joinPath("", "x.md")).toBe("x.md");
  });

  it("maps languages", () => {
    expect(languageFor("CLAUDE.md")).toBe("markdown");
    expect(languageFor(".mcp.json")).toBe("json");
    expect(languageFor("tools/run.sh")).toBe("shell");
    expect(languageFor("Makefile")).toBe("plaintext");
  });

  it("detects binary content", () => {
    expect(decodeText(new TextEncoder().encode("héllo"))).toBe("héllo");
    expect(decodeText(new Uint8Array([0xff, 0xfe, 0x00]))).toBeNull();
  });
});
