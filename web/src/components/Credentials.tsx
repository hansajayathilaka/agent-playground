import { type FormEvent, useCallback, useEffect, useState } from "react";
import { AuthType, type CredentialSet, Provider } from "../gen/platform/v1/credential_pb.js";
import { credentialClient, errorMessage } from "../lib/api.js";

const PROVIDERS: { value: Provider; label: string; needsUrl: boolean }[] = [
  { value: Provider.ANTHROPIC, label: "Anthropic API", needsUrl: false },
  { value: Provider.GATEWAY, label: "Anthropic-compatible gateway", needsUrl: true },
  { value: Provider.LITELLM, label: "LiteLLM", needsUrl: true },
];

function providerLabel(p: Provider): string {
  return PROVIDERS.find((x) => x.value === p)?.label ?? Provider[p] ?? "unknown";
}

interface FormState {
  id: string; // empty = create
  name: string;
  description: string;
  provider: Provider;
  baseUrl: string;
  authType: AuthType;
  secret: string;
  defaultModel: string;
  allowedModels: string;
  headers: string;
  makeDefault: boolean;
}

const blank: FormState = {
  id: "",
  name: "",
  description: "",
  provider: Provider.ANTHROPIC,
  baseUrl: "",
  authType: AuthType.API_KEY,
  secret: "",
  defaultModel: "",
  allowedModels: "",
  headers: "",
  makeDefault: false,
};

function toForm(c: CredentialSet): FormState {
  return {
    id: c.id,
    name: c.name,
    description: c.description,
    provider: c.provider,
    baseUrl: c.baseUrl,
    authType: c.authType,
    secret: "",
    defaultModel: c.defaultModel,
    allowedModels: c.allowedModels.join(", "),
    headers: Object.entries(c.headers)
      .map(([k, v]) => `${k}: ${v}`)
      .join("\n"),
    makeDefault: c.isDefault,
  };
}

function parseHeaders(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const i = line.indexOf(":");
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
}

export function Credentials() {
  const [sets, setSets] = useState<CredentialSet[]>([]);
  const [form, setForm] = useState<FormState | null>(null);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const r = await credentialClient.listCredentialSets({});
      setSets(r.credentialSets);
      setError("");
    } catch (err) {
      setError(errorMessage(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!form) return;
    const input = {
      name: form.name,
      description: form.description,
      provider: form.provider,
      baseUrl: form.baseUrl,
      authType: form.authType,
      headers: parseHeaders(form.headers),
      defaultModel: form.defaultModel,
      allowedModels: form.allowedModels
        .split(",")
        .map((m) => m.trim())
        .filter(Boolean),
    };
    try {
      if (form.id) {
        await credentialClient.updateCredentialSet({
          id: form.id,
          input,
          secret: form.secret ? form.secret : undefined,
        });
        if (form.makeDefault) await credentialClient.makeDefault({ id: form.id });
      } else {
        await credentialClient.createCredentialSet({
          input,
          secret: form.secret,
          makeDefault: form.makeDefault,
        });
      }
      setForm(null);
      await refresh();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const remove = async (c: CredentialSet) => {
    if (!window.confirm(`Delete credential set ${c.name}?`)) return;
    try {
      await credentialClient.deleteCredentialSet({ id: c.id });
      await refresh();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const makeDefault = async (c: CredentialSet) => {
    try {
      await credentialClient.makeDefault({ id: c.id });
      await refresh();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const needsUrl = PROVIDERS.find((p) => p.value === form?.provider)?.needsUrl ?? false;

  return (
    <section className="workspace credentials">
      <div className="workspace-head">
        <div>
          <h2>Credentials</h2>
          <span className="muted">
            Model endpoints and keys. Agents use the default set unless agent.json names one in
            "credential_set". Secrets are encrypted and never shown again.
          </span>
        </div>
        <span className="spacer" />
        <button type="button" className="primary" onClick={() => setForm({ ...blank })}>
          Add credential set
        </button>
      </div>
      {error && <p className="error pad">{error}</p>}
      <div className="cred-body">
        <table className="cred-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Provider</th>
              <th>Default model</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {sets.map((c) => (
              <tr key={c.id}>
                <td>
                  <strong>{c.name}</strong>{" "}
                  {c.isDefault && <span className="badge ok">default</span>}
                  {!c.hasSecret && <span className="badge bad">no secret</span>}
                  <div className="muted">{c.description}</div>
                </td>
                <td>
                  {providerLabel(c.provider)}
                  {c.baseUrl && <div className="muted">{c.baseUrl}</div>}
                </td>
                <td>{c.defaultModel || <span className="muted">SDK default</span>}</td>
                <td className="row-actions">
                  <button type="button" onClick={() => setForm(toForm(c))}>
                    Edit
                  </button>
                  {!c.isDefault && (
                    <button type="button" onClick={() => makeDefault(c)}>
                      Make default
                    </button>
                  )}
                  <button type="button" className="danger" onClick={() => remove(c)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
            {!sets.length && (
              <tr>
                <td colSpan={4} className="muted">
                  No credential sets yet. Add one to run agents.
                </td>
              </tr>
            )}
          </tbody>
        </table>
        {form && (
          <form className="card cred-form" onSubmit={save}>
            <h1>{form.id ? `Edit ${form.name}` : "New credential set"}</h1>
            <label>
              Name
              <input
                required
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
              />
            </label>
            <label>
              Description
              <input
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
              />
            </label>
            <label>
              Provider
              <select
                value={form.provider}
                onChange={(e) => setForm({ ...form, provider: Number(e.target.value) })}
              >
                {PROVIDERS.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Base URL {needsUrl ? "" : "(optional)"}
              <input
                required={needsUrl}
                placeholder="https://…"
                value={form.baseUrl}
                onChange={(e) => setForm({ ...form, baseUrl: e.target.value })}
              />
            </label>
            <label>
              Auth
              <select
                value={form.authType}
                onChange={(e) => setForm({ ...form, authType: Number(e.target.value) })}
              >
                <option value={AuthType.API_KEY}>API key (x-api-key)</option>
                <option value={AuthType.BEARER}>Bearer token</option>
              </select>
            </label>
            <label>
              Secret{" "}
              {form.id && <span className="muted">(leave empty to keep the current one)</span>}
              <input
                type="password"
                autoComplete="off"
                required={!form.id}
                value={form.secret}
                onChange={(e) => setForm({ ...form, secret: e.target.value })}
              />
            </label>
            <label>
              Default model (optional)
              <input
                placeholder="claude-…"
                value={form.defaultModel}
                onChange={(e) => setForm({ ...form, defaultModel: e.target.value })}
              />
            </label>
            <label>
              Allowed models (comma separated, optional)
              <input
                value={form.allowedModels}
                onChange={(e) => setForm({ ...form, allowedModels: e.target.value })}
              />
            </label>
            <label>
              Extra headers (one "Name: value" per line, optional)
              <textarea
                rows={2}
                value={form.headers}
                onChange={(e) => setForm({ ...form, headers: e.target.value })}
              />
            </label>
            <label className="inline">
              <input
                type="checkbox"
                checked={form.makeDefault}
                onChange={(e) => setForm({ ...form, makeDefault: e.target.checked })}
              />
              Default set
            </label>
            <div className="row-actions">
              <button type="submit" className="primary">
                Save
              </button>
              <button type="button" onClick={() => setForm(null)}>
                Cancel
              </button>
            </div>
          </form>
        )}
      </div>
    </section>
  );
}
