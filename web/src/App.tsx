import { useEffect, useState } from "react";
import { AgentList } from "./components/AgentList.js";
import { AgentWorkspace } from "./components/AgentWorkspace.js";
import { Login } from "./components/Login.js";
import { authClient, getToken, onAuthChange, setToken } from "./lib/api.js";

function readSlugFromHash(): string | null {
  const m = /^#\/agents\/([^/]+)/.exec(window.location.hash);
  return m?.[1] ? decodeURIComponent(m[1]) : null;
}

export function App() {
  const [signedIn, setSignedIn] = useState(Boolean(getToken()));
  const [slug, setSlug] = useState<string | null>(readSlugFromHash);
  const [listVersion, setListVersion] = useState(0);
  const [user, setUser] = useState("");

  useEffect(() => onAuthChange(setSignedIn), []);
  useEffect(() => {
    const onHash = () => setSlug(readSlugFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => {
    if (!signedIn) return;
    authClient
      .whoAmI({})
      .then((r) => setUser(r.username))
      .catch(() => setUser(""));
  }, [signedIn]);

  if (!signedIn) return <Login />;

  const select = (next: string | null) => {
    window.location.hash = next ? `#/agents/${encodeURIComponent(next)}` : "";
    setSlug(next);
  };

  const logout = async () => {
    try {
      await authClient.logout({});
    } finally {
      setToken("");
    }
  };

  return (
    <div className="app">
      <header className="topbar">
        <strong>Agent Playground</strong>
        <span className="spacer" />
        {user && <span className="muted">{user}</span>}
        <button type="button" onClick={logout}>
          Sign out
        </button>
      </header>
      <div className="main">
        <AgentList
          selected={slug}
          onSelect={select}
          version={listVersion}
          onChanged={() => setListVersion((v) => v + 1)}
        />
        {slug ? (
          <AgentWorkspace
            key={slug}
            slug={slug}
            onDeleted={() => {
              select(null);
              setListVersion((v) => v + 1);
            }}
            onManifestSaved={() => setListVersion((v) => v + 1)}
          />
        ) : (
          <div className="empty">Pick an agent, or create one.</div>
        )}
      </div>
    </div>
  );
}
