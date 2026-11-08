import Editor from "@monaco-editor/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Agent } from "../gen/platform/v1/agent_pb.js";
import { type Diagnostic, Diagnostic_Severity } from "../gen/platform/v1/common_pb.js";
import type { FileEntry } from "../gen/platform/v1/file_pb.js";
import { agentClient, Code, errorMessage, fileClient, isCode } from "../lib/api.js";
import {
  buildTree,
  decodeText,
  joinPath,
  languageFor,
  type TreeNode,
  targetDir,
} from "../lib/tree.js";
import { FileTree } from "./FileTree.js";

interface OpenFile {
  path: string;
  etag: string;
  saved: string; // text as last read/saved
  binary: boolean;
  size: number;
}

interface Props {
  slug: string;
  onDeleted: () => void;
  onManifestSaved: () => void;
}

async function readAll(slug: string, path: string): Promise<{ etag: string; bytes: Uint8Array }> {
  const parts: Uint8Array[] = [];
  let etag = "";
  for await (const msg of fileClient.readFile({ slug, path })) {
    if (msg.etag) etag = msg.etag;
    parts.push(msg.chunk);
  }
  const total = parts.reduce((n, p) => n + p.length, 0);
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const p of parts) {
    bytes.set(p, offset);
    offset += p.length;
  }
  return { etag, bytes };
}

export function AgentWorkspace({ slug, onDeleted, onManifestSaved }: Props) {
  const [agent, setAgent] = useState<Agent | null>(null);
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [showHidden, setShowHidden] = useState(false);
  const [selected, setSelected] = useState<TreeNode | null>(null);
  const [file, setFile] = useState<OpenFile | null>(null);
  const [text, setText] = useState("");
  const [diagnostics, setDiagnostics] = useState<Diagnostic[]>([]);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const textRef = useRef(text);
  textRef.current = text;

  const tree = useMemo(() => buildTree(entries), [entries]);
  const dirty = file !== null && !file.binary && text !== file.saved;

  const fail = useCallback((err: unknown) => setError(errorMessage(err)), []);

  const refreshTree = useCallback(async () => {
    const r = await fileClient.getTree({ slug, includeHidden: showHidden });
    setEntries(r.entries);
  }, [slug, showHidden]);

  const refreshAgent = useCallback(async () => {
    const r = await agentClient.getAgent({ slug });
    setAgent(r.agent ?? null);
    setDiagnostics(r.diagnostics);
  }, [slug]);

  useEffect(() => {
    refreshTree().catch(fail);
  }, [refreshTree, fail]);
  useEffect(() => {
    refreshAgent().catch(fail);
  }, [refreshAgent, fail]);

  const open = async (path: string) => {
    if (dirty && !window.confirm(`Discard unsaved changes to ${file?.path}?`)) return;
    setError("");
    setConflict(false);
    try {
      const { etag, bytes } = await readAll(slug, path);
      const decoded = decodeText(bytes);
      setFile({
        path,
        etag,
        saved: decoded ?? "",
        binary: decoded === null,
        size: bytes.length,
      });
      setText(decoded ?? "");
    } catch (err) {
      fail(err);
    }
  };

  const save = async (force = false) => {
    if (!file || file.binary) return;
    const content = textRef.current;
    setError("");
    try {
      const r = await fileClient.writeFile({
        slug,
        path: file.path,
        content: new TextEncoder().encode(content),
        ifMatch: force ? "" : file.etag,
      });
      setFile({ ...file, etag: r.etag, saved: content });
      setConflict(false);
      setStatus(`Saved ${file.path}`);
      await refreshAgent();
      if (file.path === "agent.json") onManifestSaved();
      if (r.diagnostics.length)
        setStatus(`Saved ${file.path} with ${r.diagnostics.length} issue(s)`);
    } catch (err) {
      if (isCode(err, Code.Aborted)) setConflict(true);
      else fail(err);
    }
  };

  const saveRef = useRef(save);
  saveRef.current = save;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "s") {
        e.preventDefault();
        void saveRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const baseDir = targetDir(selected?.path ?? null, selected?.isDir ?? false);

  const newFile = async () => {
    const name = window.prompt("New file path", joinPath(baseDir, ""));
    if (!name) return;
    try {
      await fileClient.writeFile({ slug, path: name, content: new Uint8Array(), createOnly: true });
      await refreshTree();
      await open(name);
    } catch (err) {
      fail(err);
    }
  };

  const newFolder = async () => {
    const name = window.prompt("New folder path", joinPath(baseDir, ""));
    if (!name) return;
    try {
      await fileClient.createDirectory({ slug, path: name });
      await refreshTree();
    } catch (err) {
      fail(err);
    }
  };

  const rename = async () => {
    if (!selected) return;
    const to = window.prompt(`Move ${selected.path} to`, selected.path);
    if (!to || to === selected.path) return;
    try {
      await fileClient.moveFile({ slug, fromPath: selected.path, toPath: to });
      if (file?.path === selected.path) setFile({ ...file, path: to });
      setSelected(null);
      await refreshTree();
    } catch (err) {
      fail(err);
    }
  };

  const remove = async () => {
    if (!selected) return;
    const what = selected.isDir ? `folder ${selected.path} and everything in it` : selected.path;
    if (!window.confirm(`Delete ${what}?`)) return;
    try {
      await fileClient.deleteFile({ slug, path: selected.path, recursive: selected.isDir });
      if (file && (file.path === selected.path || file.path.startsWith(`${selected.path}/`))) {
        setFile(null);
      }
      setSelected(null);
      await refreshTree();
      await refreshAgent();
    } catch (err) {
      fail(err);
    }
  };

  const deleteAgent = async () => {
    if (!window.confirm(`Delete agent ${slug}? Its folder moves to the trash.`)) return;
    try {
      await agentClient.deleteAgent({ slug });
      onDeleted();
    } catch (err) {
      fail(err);
    }
  };

  const errors = diagnostics.filter((d) => d.severity === Diagnostic_Severity.ERROR).length;

  return (
    <section className="workspace">
      <div className="workspace-head">
        <div>
          <h2>{agent?.name ?? slug}</h2>
          <span className="muted">{agent?.description}</span>
        </div>
        <span className="spacer" />
        <span className={errors ? "badge bad" : "badge ok"}>
          {errors ? `${errors} error(s): runs blocked` : "valid"}
        </span>
        <button type="button" className="danger" onClick={deleteAgent}>
          Delete agent
        </button>
      </div>
      <div className="workspace-body">
        <aside className="files">
          <div className="toolbar">
            <button type="button" onClick={newFile} title="New file">
              + File
            </button>
            <button type="button" onClick={newFolder} title="New folder">
              + Folder
            </button>
            <button type="button" onClick={rename} disabled={!selected}>
              Move
            </button>
            <button type="button" onClick={remove} disabled={!selected}>
              Delete
            </button>
          </div>
          <label className="hidden-toggle">
            <input
              type="checkbox"
              checked={showHidden}
              onChange={(e) => setShowHidden(e.target.checked)}
            />
            Show platform files (state/, .git/)
          </label>
          <FileTree
            nodes={tree}
            selected={selected?.path ?? null}
            dirty={dirty ? (file?.path ?? null) : null}
            onSelect={(n) => {
              setSelected(n);
              if (!n.isDir) void open(n.path);
            }}
          />
        </aside>
        <div className="editor">
          {file ? (
            <>
              <div className="editor-head">
                <code>{file.path}</code>
                {dirty && <span className="muted"> (unsaved)</span>}
                <span className="spacer" />
                <span className="muted">{status}</span>
                <button
                  type="button"
                  className="primary"
                  onClick={() => save()}
                  disabled={!dirty || file.binary}
                >
                  Save
                </button>
              </div>
              {conflict && (
                <div className="banner">
                  This file changed on disk since you opened it.
                  <button type="button" onClick={() => open(file.path)}>
                    Reload
                  </button>
                  <button type="button" className="danger" onClick={() => save(true)}>
                    Overwrite
                  </button>
                </div>
              )}
              {file.binary ? (
                <div className="empty">Binary file ({file.size} bytes) can't be edited here.</div>
              ) : (
                <div className="editor-host">
                  <Editor
                    height="100%"
                    path={`/${slug}/${file.path}`}
                    language={languageFor(file.path)}
                    value={text}
                    onChange={(v) => setText(v ?? "")}
                    theme="vs-dark"
                    options={{ minimap: { enabled: false }, fontSize: 13, wordWrap: "on" }}
                  />
                </div>
              )}
            </>
          ) : (
            <div className="empty">Select a file to edit.</div>
          )}
          {error && <p className="error pad">{error}</p>}
          {diagnostics.length > 0 && (
            <ul className="diagnostics">
              {diagnostics.map((d) => (
                <li
                  key={`${d.path}${d.location}${d.message}`}
                  className={d.severity === Diagnostic_Severity.ERROR ? "bad" : "warn"}
                >
                  <button type="button" className="link" onClick={() => open(d.path)}>
                    {d.path}
                    {d.location}
                  </button>{" "}
                  {d.message}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </section>
  );
}
