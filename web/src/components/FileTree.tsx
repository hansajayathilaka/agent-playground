import { useState } from "react";
import type { TreeNode } from "../lib/tree.js";

interface Props {
  nodes: TreeNode[];
  selected: string | null;
  dirty: string | null;
  onSelect: (node: TreeNode) => void;
}

export function FileTree({ nodes, selected, dirty, onSelect }: Props) {
  const [open, setOpen] = useState<Set<string>>(() => new Set([".claude", "memory"]));

  const toggle = (path: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  const render = (list: TreeNode[], depth: number) =>
    list.map((n) => (
      <li key={n.path}>
        <button
          type="button"
          className={n.path === selected ? "node active" : "node"}
          style={{ paddingLeft: `${0.5 + depth * 0.9}rem` }}
          onClick={() => {
            if (n.isDir) toggle(n.path);
            onSelect(n);
          }}
          title={n.path}
        >
          <span className="icon">{n.isDir ? (open.has(n.path) ? "▾" : "▸") : "·"}</span>
          {n.name}
          {n.path === dirty && <span className="dot"> ●</span>}
        </button>
        {n.isDir && open.has(n.path) && n.children.length > 0 && (
          <ul>{render(n.children, depth + 1)}</ul>
        )}
      </li>
    ));

  return <ul className="tree">{render(nodes, 0)}</ul>;
}
