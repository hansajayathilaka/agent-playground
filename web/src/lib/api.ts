import { Code, ConnectError, createClient, type Interceptor } from "@connectrpc/connect";
import { createGrpcWebTransport } from "@connectrpc/connect-web";
import { AgentService } from "../gen/platform/v1/agent_pb.js";
import { AuthService } from "../gen/platform/v1/auth_pb.js";
import { FileService } from "../gen/platform/v1/file_pb.js";

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

export function errorMessage(err: unknown): string {
  return ConnectError.from(err).rawMessage;
}

export function isCode(err: unknown, code: Code): boolean {
  return ConnectError.from(err).code === code;
}

export { Code };
