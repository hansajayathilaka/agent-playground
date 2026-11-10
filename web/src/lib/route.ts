// Hash routes, so a page refresh lands on the same agent, tab and conversation.
export type Route =
  | { page: "home" }
  | { page: "credentials" }
  | {
      page: "agent";
      slug: string;
      view: "files" | "chat" | "history";
      // Conversation id for "chat", commit id for "history".
      conversationId: string | null;
    };

export function parseRoute(hash: string): Route {
  if (hash === "#/credentials") return { page: "credentials" };
  const m = /^#\/agents\/([^/]+)(?:\/(files|chat|history)(?:\/([^/]+))?)?$/.exec(hash);
  if (!m?.[1]) return { page: "home" };
  const view = m[2] === "chat" || m[2] === "history" ? m[2] : "files";
  return {
    page: "agent",
    slug: decodeURIComponent(m[1]),
    view,
    conversationId: view !== "files" && m[3] ? decodeURIComponent(m[3]) : null,
  };
}

export function routeHash(r: Route): string {
  if (r.page === "home") return "";
  if (r.page === "credentials") return "#/credentials";
  const base = `#/agents/${encodeURIComponent(r.slug)}/${r.view}`;
  return r.conversationId ? `${base}/${encodeURIComponent(r.conversationId)}` : base;
}
