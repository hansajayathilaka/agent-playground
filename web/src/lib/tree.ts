import { type FileEntry, FileEntry_Kind } from "../gen/platform/v1/file_pb.js";

export interface TreeNode {
  name: string;
  path: string;
  isDir: boolean;
  size: bigint;
  children: TreeNode[];
}

/** Build a nested tree from the flat, depth-first list GetTree returns. */
export function buildTree(entries: readonly FileEntry[]): TreeNode[] {
  const root: TreeNode[] = [];
  const dirs = new Map<string, TreeNode>();
  for (const e of entries) {
    const slash = e.path.lastIndexOf("/");
    const parentPath = slash < 0 ? "" : e.path.slice(0, slash);
    const node: TreeNode = {
      name: e.path.slice(slash + 1),
      path: e.path,
      isDir: e.kind === FileEntry_Kind.DIRECTORY,
      size: e.size,
      children: [],
    };
    if (node.isDir) dirs.set(node.path, node);
    const siblings = parentPath ? dirs.get(parentPath)?.children : root;
    (siblings ?? root).push(node);
  }
  const sort = (nodes: TreeNode[]): void => {
    nodes.sort((a, b) => Number(b.isDir) - Number(a.isDir) || a.name.localeCompare(b.name));
    for (const n of nodes) sort(n.children);
  };
  sort(root);
  return root;
}

/** Folder a new file should go in, given the current selection. */
export function targetDir(selected: string | null, isDir: boolean): string {
  if (!selected) return "";
  if (isDir) return selected;
  const slash = selected.lastIndexOf("/");
  return slash < 0 ? "" : selected.slice(0, slash);
}

export function joinPath(dir: string, name: string): string {
  const clean = name.replace(/^\/+/, "");
  return dir ? `${dir}/${clean}` : clean;
}

const LANGUAGES: Record<string, string> = {
  md: "markdown",
  json: "json",
  py: "python",
  ts: "typescript",
  tsx: "typescript",
  js: "javascript",
  mjs: "javascript",
  sh: "shell",
  bash: "shell",
  yml: "yaml",
  yaml: "yaml",
  toml: "ini",
  html: "html",
  css: "css",
  sql: "sql",
};

export function languageFor(path: string): string {
  const name = path.slice(path.lastIndexOf("/") + 1);
  const dot = name.lastIndexOf(".");
  if (dot <= 0 && name !== ".mcp.json") return "plaintext";
  return LANGUAGES[name.slice(dot + 1).toLowerCase()] ?? "plaintext";
}

/** Decode UTF-8 text, or null when the bytes are not valid UTF-8 (binary). */
export function decodeText(bytes: Uint8Array): string | null {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
}
