export type AgentInfo = {
  name: string
  description: string
  tags: string[]
  skills: string[]
  subagents: string[]
  mcpServers: string[]
  tools: string[]
  connectors: string[]
}

export type RunInfo = {
  id: string
  agent: string
  task: string
  status: string
  activity: string
  startedAt: string
  updatedAt: string
  finishedAt: string
  error: string
}

export type Snapshot = {
  /** Absolute workspace root, or '' when the session is outside one. */
  root: string
  name: string
  /** The agent whose folder the session runs in, or ''. */
  here: string
  agents: AgentInfo[]
  runs: RunInfo[]
  catalog: string[]
  scannedAt: number
}

declare module 'claude-code' {
  interface PluginState {
    'playground-hud': { snapshot: Shaped<Snapshot> }
  }
}
