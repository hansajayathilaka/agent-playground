import { type FormEvent, useEffect, useState } from "react";
import type { Template } from "../gen/platform/v1/agent_pb.js";
import { agentClient, errorMessage } from "../lib/api.js";

export function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 50);
}

export function NewAgentForm({ onCreated }: { onCreated: (slug: string) => void }) {
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

  return (
    <form className="new-agent" onSubmit={submit}>
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
