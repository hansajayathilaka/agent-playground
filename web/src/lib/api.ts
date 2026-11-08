import { Code, ConnectError, createClient, type Interceptor } from "@connectrpc/connect";
import { createGrpcWebTransport } from "@connectrpc/connect-web";
import { AgentService } from "../gen/platform/v1/agent_pb.js";
import { AuthService } from "../gen/platform/v1/auth_pb.js";
import { CredentialService } from "../gen/platform/v1/credential_pb.js";
import { type Event, EventService } from "../gen/platform/v1/event_pb.js";
import { FileService } from "../gen/platform/v1/file_pb.js";
import { ConversationService, RunService } from "../gen/platform/v1/run_pb.js";

const TOKEN_KEY = "agent-playground.token";

let token = "";
try {
  token = localStorage.getItem(TOKEN_KEY) ?? "";
} catch {
  // storage blocked: the session just won't survive a reload
}

const listeners = new Set<(signedIn: boolean) => void>();

export function getToken(): string {
  return token;
}

export function setToken(value: string): void {
  token = value;
  try {
    if (value) localStorage.setItem(TOKEN_KEY, value);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // ignore
  }
  for (const fn of listeners) fn(Boolean(value));
}

export function onAuthChange(fn: (signedIn: boolean) => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

const auth: Interceptor = (next) => async (req) => {
  if (token) req.header.set("authorization", `Bearer ${token}`);
  try {
    return await next(req);
  } catch (err) {
    if (ConnectError.from(err).code === Code.Unauthenticated && token) setToken("");
    throw err;
  }
};

// Envoy serves the UI and the API on the same origin and translates gRPC-Web.
const transport = createGrpcWebTransport({
  baseUrl: import.meta.env.VITE_API_URL ?? window.location.origin,
  interceptors: [auth],
});

export const authClient = createClient(AuthService, transport);
export const agentClient = createClient(AgentService, transport);
export const fileClient = createClient(FileService, transport);
export const runClient = createClient(RunService, transport);
export const conversationClient = createClient(ConversationService, transport);
export const eventClient = createClient(EventService, transport);
export const credentialClient = createClient(CredentialService, transport);

/**
 * Follow a conversation's events from ``afterOffset``, reconnecting with
 * backoff after any disconnect and resuming from the last offset seen.
 * Events are delivered in batches (one per animation frame at most) so a
 * long replay renders once instead of once per token. Returns a stop function.
 */
export function watchConversation(
  conversationId: string,
  afterOffset: bigint,
  onEvents: (events: Event[]) => void,
  onConnection: (connected: boolean, error: string) => void,
): () => void {
  const abort = new AbortController();
  let last = afterOffset;
  let pending: Event[] = [];
  let scheduled = false;
  const flush = () => {
    scheduled = false;
    if (!pending.length || abort.signal.aborted) return;
    const batch = pending;
    pending = [];
    onEvents(batch);
  };
  const run = async () => {
    let delay = 500;
    while (!abort.signal.aborted) {
      try {
        const stream = eventClient.watchEvents(
          { conversationId, afterOffset: last },
          { signal: abort.signal },
        );
        onConnection(true, "");
        for await (const msg of stream) {
          const ev = msg.event;
          if (!ev) continue;
          delay = 500;
          if (ev.offset > 0n) {
            last = ev.offset;
            pending.push(ev);
            if (!scheduled) {
              scheduled = true;
              requestAnimationFrame(flush);
            }
          }
        }
      } catch (err) {
        if (abort.signal.aborted) return;
        const e = ConnectError.from(err);
        if (e.code === Code.NotFound || e.code === Code.Unauthenticated) {
          onConnection(false, e.rawMessage);
          return;
        }
        onConnection(false, "reconnecting…");
      }
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay * 2, 10_000);
    }
  };
  void run();
  return () => abort.abort();
}

export function errorMessage(err: unknown): string {
  return ConnectError.from(err).rawMessage;
}

export function isCode(err: unknown, code: Code): boolean {
  return ConnectError.from(err).code === code;
}

export { Code };
