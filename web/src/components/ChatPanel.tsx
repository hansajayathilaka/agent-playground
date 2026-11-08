import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Event } from "../gen/platform/v1/event_pb.js";
import type { Conversation } from "../gen/platform/v1/run_pb.js";
import { conversationClient, errorMessage, runClient, watchConversation } from "../lib/api.js";
import {
  applyBatch,
  childrenOf,
  emptyTranscript,
  type Item,
  isActive,
  type Transcript,
  type Turn,
} from "../lib/transcript.js";

interface Props {
  slug: string;
  conversationId: string | null;
  onSelect: (conversationId: string | null) => void;
}

function shortTime(ts: { seconds: bigint } | undefined): string {
  if (!ts) return "";
  return new Date(Number(ts.seconds) * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function ChatPanel({ slug, conversationId, onSelect }: Props) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [error, setError] = useState("");

  const refreshList = useCallback(async () => {
    try {
      const r = await conversationClient.listConversations({ agentSlug: slug });
      setConversations(r.conversations);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [slug]);

  useEffect(() => {
    void refreshList();
  }, [refreshList]);

  return (
    <div className="chat">
      <aside className="conversations">
        <div className="toolbar">
          <button type="button" className="primary" onClick={() => onSelect(null)}>
            + New chat
          </button>
        </div>
        {error && <p className="error pad">{error}</p>}
        <ul>
          {conversations.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                className={c.id === conversationId ? "agent active" : "agent"}
                onClick={() => onSelect(c.id)}
              >
                <span className="ellipsis">{c.title || "Untitled"}</span>
                <small className="muted">{shortTime(c.updatedAt)}</small>
              </button>
            </li>
          ))}
          {!conversations.length && <li className="muted pad">No conversations yet.</li>}
        </ul>
      </aside>
      <ConversationView
        key={conversationId ?? "new"}
        slug={slug}
        conversationId={conversationId}
        onCreated={(id) => {
          onSelect(id);
          void refreshList();
        }}
        onTurnDone={refreshList}
      />
    </div>
  );
}

interface ConversationProps {
  slug: string;
  conversationId: string | null;
  onCreated: (id: string) => void;
  onTurnDone: () => void;
}

function ConversationView({ slug, conversationId, onCreated, onTurnDone }: ConversationProps) {
  const [transcript, setTranscript] = useState<Transcript>(emptyTranscript);
  const [connection, setConnection] = useState("");
  const [queue, setQueue] = useState<string[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const lastStatus = useRef("");

  useEffect(() => {
    if (!conversationId) return;
    return watchConversation(
      conversationId,
      0n,
      (events: Event[]) => setTranscript((t) => applyBatch(t, events)),
      (connected, msg) => setConnection(connected ? "" : msg),
    );
  }, [conversationId]);

  const active = transcript.turns.find((t) => isActive(t.status));
  const statusKey = transcript.turns.map((t) => t.status).join(",");

  // Refresh "waiting on" info and the conversation list when a turn changes state.
  useEffect(() => {
    if (!conversationId || statusKey === lastStatus.current) return;
    const changed = lastStatus.current !== "";
    lastStatus.current = statusKey;
    conversationClient
      .getConversation({ conversationId })
      .then((r) => setQueue(r.agentQueue))
      .catch(() => setQueue([]));
    if (changed) onTurnDone();
  }, [conversationId, statusKey, onTurnDone]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });

  const onScroll = () => {
    const el = scroller.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const send = async () => {
    const text = draft.trim();
    if (!text || busy) return;
    setBusy(true);
    setError("");
    try {
      let id = conversationId;
      if (!id) {
        const r = await conversationClient.createConversation({ agentSlug: slug });
        id = r.conversation?.id ?? "";
      }
      await conversationClient.sendMessage({ conversationId: id, text });
      setDraft("");
      stick.current = true;
      if (id !== conversationId) onCreated(id);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    if (!active) return;
    try {
      await runClient.cancelRun({ runId: active.runId });
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const ahead = active?.status === "queued" ? queue.indexOf(active.runId) : -1;

  return (
    <section className="conversation">
      <div className="transcript" ref={scroller} onScroll={onScroll}>
        {!conversationId && (
          <div className="empty">Start a conversation with {slug}. Each message is one run.</div>
        )}
        {transcript.turns.map((turn) => (
          <TurnView key={turn.runId} turn={turn} />
        ))}
      </div>
      {connection && <div className="banner">{connection}</div>}
      {active?.status === "queued" && (
        <div className="banner muted">
          {ahead > 0
            ? `Queued behind ${ahead} other run(s) for this agent.`
            : "Queued: waiting for a worker or for this agent's current run to finish."}
        </div>
      )}
      {error && <p className="error pad">{error}</p>}
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <textarea
          value={draft}
          placeholder={active ? "Queue another message…" : "Message the agent (Ctrl+Enter to send)"}
          rows={3}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              void send();
            }
          }}
        />
        <div className="composer-actions">
          {active && (
            <button type="button" className="danger" onClick={cancel}>
              Stop
            </button>
          )}
          <button type="submit" className="primary" disabled={busy || !draft.trim()}>
            Send
          </button>
        </div>
      </form>
    </section>
  );
}

const STATUS_LABEL: Record<string, string> = {
  queued: "queued",
  running: "running…",
  succeeded: "done",
  failed: "failed",
  cancelled: "stopped",
  interrupted: "interrupted",
};

function TurnView({ turn }: { turn: Turn }) {
  const bad = turn.status === "failed" || turn.status === "interrupted";
  return (
    <div className="turn">
      <div className="bubble user">{turn.prompt}</div>
      <div className="bubble agent">
        <Items turn={turn} parent="" />
        {turn.status === "running" && !turn.items.length && (
          <span className="muted">Thinking…</span>
        )}
      </div>
      <div className={bad ? "turn-meta bad" : "turn-meta muted"}>
        <span className={`status ${turn.status}`}>{STATUS_LABEL[turn.status] ?? turn.status}</span>
        {turn.error && turn.status !== "cancelled" && <span> · {turn.error}</span>}
        {turn.costUsd !== undefined && turn.costUsd > 0 && (
          <span> · ${turn.costUsd.toFixed(4)}</span>
        )}
        {turn.numTurns !== undefined && <span> · {turn.numTurns} step(s)</span>}
      </div>
    </div>
  );
}

function Items({ turn, parent }: { turn: Turn; parent: string }) {
  return (
    <>
      {childrenOf(turn, parent).map((item) => (
        <ItemView key={item.key} turn={turn} item={item} />
      ))}
    </>
  );
}

function prettyJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function ItemView({ turn, item }: { turn: Turn; item: Item }) {
  if (item.kind === "text") {
    return <div className={item.final ? "text" : "text streaming"}>{item.text}</div>;
  }
  if (item.kind === "thinking") {
    return (
      <details className="thinking">
        <summary>Thinking</summary>
        <div className="text">{item.text}</div>
      </details>
    );
  }
  if (item.kind === "notice") {
    return <div className={`notice ${item.level}`}>{item.message}</div>;
  }
  if (item.kind !== "tool") return null;
  const nested = childrenOf(turn, item.toolUseId);
  const pending = item.result === undefined;
  return (
    <details className={item.isError ? "tool bad" : "tool"} open={nested.length > 0 && pending}>
      <summary>
        <code>{item.name || "tool"}</code>
        {item.subagent && <span className="muted"> · subagent: {item.subagent.description}</span>}
        {pending ? <span className="muted"> · running…</span> : item.isError && " · error"}
      </summary>
      {item.input && <pre className="tool-io">{prettyJson(item.input)}</pre>}
      {nested.length > 0 && (
        <div className="subagent">
          <Items turn={turn} parent={item.toolUseId} />
        </div>
      )}
      {item.result !== undefined && <pre className="tool-io">{item.result}</pre>}
    </details>
  );
}
