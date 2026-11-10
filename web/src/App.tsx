import { useEffect, useState } from "react";
import { AgentList } from "./components/AgentList.js";
import { AgentWorkspace, type WorkspaceView } from "./components/AgentWorkspace.js";
import { Credentials } from "./components/Credentials.js";
import { Login } from "./components/Login.js";
import { authClient, getToken, onAuthChange, setToken } from "./lib/api.js";
import { parseRoute, type Route, routeHash } from "./lib/route.js";

export function App() {
  const [signedIn, setSignedIn] = useState(Boolean(getToken()));
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  const [listVersion, setListVersion] = useState(0);
  const [user, setUser] = useState("");

  useEffect(() => onAuthChange(setSignedIn), []);
  useEffect(() => {
    const onHash = () => setRoute(parseRoute(window.location.hash));
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

  const go = (next: Route) => {
    window.location.hash = routeHash(next);
    setRoute(next);
  };
  const slug = route.page === "agent" ? route.slug : null;
  const select = (next: string | null) =>
    go(next ? { page: "agent", slug: next, view: "chat", conversationId: null } : { page: "home" });

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
        <button
          type="button"
          className={route.page === "credentials" ? "tab active" : "tab"}
          onClick={() => go({ page: "credentials" })}
        >
          Credentials
        </button>
        {user && <span className="muted">{user}</span>}
        <button type="button" onClick={logout}>
          Sign out
        </button>
      </header>
      <div className="main">
        <AgentList
          selected={slug}
          onSelect={select}
          onOpenConversation={(agentSlug, conversationId) =>
            go({ page: "agent", slug: agentSlug, view: "chat", conversationId })
          }
          version={listVersion}
          onChanged={() => setListVersion((v) => v + 1)}
        />
        {route.page === "credentials" ? (
          <Credentials />
        ) : route.page === "agent" ? (
          <AgentWorkspace
            key={route.slug}
            slug={route.slug}
            view={route.view}
            conversationId={route.conversationId}
            onNavigate={(view: WorkspaceView, conversationId: string | null) =>
              go({ page: "agent", slug: route.slug, view, conversationId })
            }
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
