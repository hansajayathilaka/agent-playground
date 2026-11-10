import { timestampDate } from "@bufbuild/protobuf/wkt";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  type Commit,
  Commit_Actor,
  type FileChange,
  FileChange_Change,
} from "../gen/platform/v1/file_pb.js";
import { errorMessage, fileClient } from "../lib/api.js";
import { parsePatch, relativeTime } from "../lib/diff.js";

const ACTOR: Record<Commit_Actor, string> = {
  [Commit_Actor.UNSPECIFIED]: "",
  [Commit_Actor.USER]: "you",
  [Commit_Actor.BUILDER]: "Builder",
  [Commit_Actor.AGENT]: "agent run",
  [Commit_Actor.PLATFORM]: "platform",
};

const CHANGE: Record<FileChange_Change, string> = {
  [FileChange_Change.UNSPECIFIED]: "?",
  [FileChange_Change.ADDED]: "A",
  [FileChange_Change.MODIFIED]: "M",
  [FileChange_Change.DELETED]: "D",
  [FileChange_Change.RENAMED]: "R",
};

interface Props {
  slug: string;
  commitId: string | null;
  onSelect: (commitId: string) => void;
  onRestored: () => void;
}

function FileDiff({ change, onUndo }: { change: FileChange; onUndo: (() => void) | null }) {
  const lines = parsePatch(change.patch);
  return (
    <section className="file-diff">
      <header>
        <span className={`change change-${CHANGE[change.change]}`}>{CHANGE[change.change]}</span>
        <code>
          {change.oldPath ? `${change.oldPath} → ` : ""}
          {change.path}
        </code>
        <span className="muted">
          {change.binary ? "binary" : `+${change.additions} −${change.deletions}`}
        </span>
        <span className="spacer" />
        {onUndo && (
          <button type="button" onClick={onUndo} title="Put this file back as it was before">
            Undo in this file
          </button>
        )}
      </header>
      {change.binary ? (
        <p className="muted pad">Binary file.</p>
      ) : (
        <pre className="patch">
          {lines.map((l, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: lines are static per patch
            <div key={i} className={`dl dl-${l.kind}`}>
              <span className="ln">{l.oldNo ?? ""}</span>
              <span className="ln">{l.newNo ?? ""}</span>
              <span className="dt">
                {l.kind === "add" ? "+" : l.kind === "del" ? "−" : " "}
                {l.text}
              </span>
            </div>
          ))}
          {change.truncated && <div className="dl dl-meta">… diff cut short</div>}
        </pre>
      )}
    </section>
  );
}

export function HistoryPanel({ slug, commitId, onSelect, onRestored }: Props) {
  const [commits, setCommits] = useState<Commit[]>([]);
  const [changes, setChanges] = useState<FileChange[]>([]);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");

  const refresh = useCallback(async () => {
    const r = await fileClient.listHistory({ slug, limit: 200 });
    setCommits(r.commits);
    return r.commits;
  }, [slug]);

  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;

  useEffect(() => {
    refresh().catch((err: unknown) => setError(errorMessage(err)));
  }, [refresh]);

  // Open the latest change when no commit is in the URL.
  useEffect(() => {
    if (!commitId && commits[0]) selectRef.current(commits[0].id);
  }, [commitId, commits]);

  useEffect(() => {
    if (!commitId) {
      setChanges([]);
      return;
    }
    setError("");
    fileClient
      .diffAgent({ slug, toCommit: commitId })
      .then((r) => setChanges(r.changes))
      .catch((err: unknown) => setError(errorMessage(err)));
  }, [slug, commitId]);

  const selected = commits.find((c) => c.id === commitId) ?? null;
  const index = selected ? commits.indexOf(selected) : -1;
  // The version just before the selected change, to undo it.
  const before = index >= 0 ? (commits[index + 1] ?? null) : null;

  const restore = async (target: Commit, paths: string[] = []) => {
    const what = paths.length ? paths.join(" and ") : "the whole agent folder";
    const when = target.time ? relativeTime(timestampDate(target.time)) : "earlier";
    if (
      !window.confirm(
        `Restore ${what} to the version from ${when}? This is recorded as a new change.`,
      )
    )
      return;
    setError("");
    try {
      let last: Commit | undefined;
      for (const path of paths.length ? paths : [""]) {
        last = (await fileClient.rollbackAgent({ slug, commit: target.id, path })).commit;
      }
      await refresh();
      setStatus(last ? last.message : "Restored");
      if (last) onSelect(last.id);
      onRestored();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <div className="history">
      <aside className="commits">
        {commits.map((c) => (
          <button
            type="button"
            key={c.id}
            className={c.id === commitId ? "commit active" : "commit"}
            onClick={() => onSelect(c.id)}
          >
            <span className="ellipsis">{c.message}</span>
            <small className="muted">
              {ACTOR[c.actor] || c.author} · {c.time ? relativeTime(timestampDate(c.time)) : ""}
            </small>
          </button>
        ))}
        {!commits.length && !error && <p className="muted pad">No history yet.</p>}
      </aside>
      <div className="commit-view">
        {selected ? (
          <>
            <div className="editor-head">
              <div>
                <strong>{selected.message}</strong>
                <div className="muted">
                  {selected.author}
                  {ACTOR[selected.actor] && ACTOR[selected.actor] !== selected.author
                    ? ` (${ACTOR[selected.actor]})`
                    : ""}{" "}
                  · {selected.time ? timestampDate(selected.time).toLocaleString() : ""} ·{" "}
                  <code>{selected.id.slice(0, 12)}</code>
                  {selected.runId && (
                    <>
                      {" "}
                      · run <code>{selected.runId.slice(0, 8)}</code>
                    </>
                  )}
                </div>
              </div>
              <span className="spacer" />
              <span className="muted">{status}</span>
              {before && (
                <button
                  type="button"
                  onClick={() => restore(before)}
                  title="Restore the folder to the version before this change"
                >
                  Undo this change
                </button>
              )}
              {index > 0 && (
                <button type="button" className="primary" onClick={() => restore(selected)}>
                  Restore this version
                </button>
              )}
            </div>
            {error && <p className="error pad">{error}</p>}
            <div className="diffs">
              {changes.map((ch) => (
                <FileDiff
                  key={`${ch.oldPath}:${ch.path}`}
                  change={ch}
                  onUndo={
                    before
                      ? () => restore(before, ch.oldPath ? [ch.oldPath, ch.path] : [ch.path])
                      : null
                  }
                />
              ))}
              {!changes.length && !error && <p className="muted pad">No file changes.</p>}
            </div>
          </>
        ) : (
          <div className="empty">{error || "Select a change to see what it did."}</div>
        )}
      </div>
    </div>
  );
}
