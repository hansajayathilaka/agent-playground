import { useEffect, useState } from "react";
import type { Agent } from "../gen/platform/v1/agent_pb.js";
import { agentClient, errorMessage } from "../lib/api.js";
import { BUILDER_SLUG, NewAgentForm } from "./NewAgentForm.js";

interface Props {
  selected: string | null;
  onSelect: (slug: string) => void;
  onOpenConversation: (slug: string, conversationId: string) => void;
  version: number;
  onChanged: () => void;
}

export function AgentList({ selected, onSelect, onOpenConversation, version, onChanged }: Props) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);

  // biome-ignore lint/correctness/useExhaustiveDependencies: version and selected are refresh triggers (the Builder may have added agents)
  useEffect(() => {
    agentClient
      .listAgents({})
      .then((r) => {
        setAgents(r.agents);
        setError("");
      })
      .catch((err: unknown) => setError(errorMessage(err)));
  }, [version, selected]);

  return (
    <nav className="sidebar">
      <div className="sidebar-head">
        <span>Agents</span>
        <button type="button" onClick={() => setCreating((c) => !c)}>
          {creating ? "Cancel" : "New"}
        </button>
      </div>
      {creating && (
        <NewAgentForm
          onCreated={(slug) => {
            setCreating(false);
            onChanged();
            onSelect(slug);
          }}
          onBuilderStarted={
            agents.some((a) => a.slug === BUILDER_SLUG)
              ? (conversationId) => {
                  setCreating(false);
                  onOpenConversation(BUILDER_SLUG, conversationId);
                }
              : null
          }
        />
      )}
      {error && <p className="error">{error}</p>}
      <ul>
        {agents.map((a) => (
          <li key={a.slug}>
            <button
              type="button"
              className={a.slug === selected ? "agent active" : "agent"}
              onClick={() => onSelect(a.slug)}
              title={a.description}
            >
              <span>{a.name}</span>
              <small className="muted">{a.slug}</small>
            </button>
          </li>
        ))}
        {!agents.length && !error && <li className="muted pad">No agents yet.</li>}
      </ul>
    </nav>
  );
}
