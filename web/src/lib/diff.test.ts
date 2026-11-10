import { describe, expect, it } from "vitest";
import { parsePatch, relativeTime } from "./diff.js";

describe("parsePatch", () => {
  it("skips headers and numbers lines", () => {
    const patch = [
      "diff --git a/CLAUDE.md b/CLAUDE.md",
      "index 1..2 100644",
      "--- a/CLAUDE.md",
      "+++ b/CLAUDE.md",
      "@@ -1,2 +1,2 @@ title",
      " # A",
      "-old",
      "+new",
      "\\ No newline at end of file",
      "",
    ].join("\n");
    const lines = parsePatch(patch);
    expect(lines.map((l) => l.kind)).toEqual(["hunk", "ctx", "del", "add", "meta"]);
    expect(lines[1]).toMatchObject({ text: "# A", oldNo: 1, newNo: 1 });
    expect(lines[2]).toMatchObject({ text: "old", oldNo: 2, newNo: null });
    expect(lines[3]).toMatchObject({ text: "new", oldNo: null, newNo: 2 });
  });

  it("handles an empty patch", () => {
    expect(parsePatch("")).toEqual([]);
  });
});

describe("relativeTime", () => {
  const now = new Date("2026-11-10T12:00:00Z");
  it("rounds to friendly units", () => {
    expect(relativeTime(new Date("2026-11-10T11:59:50Z"), now)).toBe("just now");
    expect(relativeTime(new Date("2026-11-10T11:50:00Z"), now)).toBe("10 minutes ago");
    expect(relativeTime(new Date("2026-11-10T09:00:00Z"), now)).toBe("3 hours ago");
    expect(relativeTime(new Date("2026-11-09T12:00:00Z"), now)).toBe("yesterday");
  });
});
