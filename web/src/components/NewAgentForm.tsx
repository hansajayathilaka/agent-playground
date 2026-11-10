import { type FormEvent, useEffect, useState } from "react";
import type { Template } from "../gen/platform/v1/agent_pb.js";
import { agentClient, errorMessage, runClient } from "../lib/api.js";

export const BUILDER_SLUG = "_builder";

export function builderPrompt(description: string): string {
  return `Create a new agent: ${description.trim()}`;
}

export function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 50);
}

interface Props {
  onCreated: (slug: string) => void;
  // Set when the Builder agent exists: the form can hand it a description.
  onBuilderStarted: ((conversationId: string) => void) | null;
}

export function NewAgentForm({ onCreated, onBuilderStarted }: Props) {
  const [mode, setMode] = useState<"builder" | "template">(
    onBuilderStarted ? "builder" : "template",
  );
  const [idea, setIdea] = useState("");
  const [busy, setBusy] = useState(false);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [description, setDescription] = useState("");
  const [template, setTemplate] = useState("blank");
  const [error, setError] = useState("");

  useEffect(() => {
    agentClient
      .listTemplates({})
      .then((r) => setTemplates(r.templates))
      .catch((err: unknown) => setError(errorMessage(err)));
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      const r = await agentClient.createAgent({ slug, name, description, template });
      onCreated(r.agent?.slug ?? slug);
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const askBuilder = async (e: FormEvent) => {
    e.preventDefault();
    if (!onBuilderStarted) return;
    setBusy(true);
    setError("");
    try {
      const r = await runClient.startRun({ agentSlug: BUILDER_SLUG, prompt: builderPrompt(idea) });
      if (r.conversation) onBuilderStarted(r.conversation.id);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const switcher = onBuilderStarted && (
    <div className="tabs" role="tablist">
      <button
        type="button"
        role="tab"
        aria-selected={mode === "builder"}
        className={mode === "builder" ? "tab active" : "tab"}
        onClick={() => setMode("builder")}
      >
        Describe it
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={mode === "template"}
        className={mode === "template" ? "tab active" : "tab"}
        onClick={() => setMode("template")}
      >
        From a template
      </button>
    </div>
  );

  if (mode === "builder" && onBuilderStarted) {
    return (
      <form className="new-agent" onSubmit={askBuilder}>
        {switcher}
        <textarea
          placeholder="What should the agent do? One line is enough, for example: summarise new GitHub issues every morning"
          value={idea}
          onChange={(e) => setIdea(e.target.value)}
          rows={4}
          required
        />
        {error && <p className="error">{error}</p>}
        <button type="submit" className="primary" disabled={busy || !idea.trim()}>
          Ask the Builder
        </button>
        <small className="muted">The Builder creates the agent, tests it and reports back.</small>
      </form>
    );
  }

  return (
    <form className="new-agent" onSubmit={submit}>
      {switcher}
      <input
        placeholder="Name"
        value={name}
        onChange={(e) => {
          setName(e.target.value);
          if (!slugTouched) setSlug(slugify(e.target.value));
        }}
        required
      />
      <input
        placeholder="slug"
        value={slug}
        onChange={(e) => {
          setSlugTouched(true);
          setSlug(e.target.value);
        }}
        pattern="[a-z0-9]([a-z0-9\-]*[a-z0-9])?"
        required
      />
      <textarea
        placeholder="Description"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        rows={2}
      />
      <select value={template} onChange={(e) => setTemplate(e.target.value)}>
        {templates.map((t) => (
          <option key={t.name} value={t.name} title={t.description}>
            {t.name}
          </option>
        ))}
      </select>
      {error && <p className="error">{error}</p>}
      <button type="submit" className="primary">
        Create
      </button>
    </form>
  );
}
