import type { EngineInterface } from 'claude-code'

import type { AgentInfo, RunInfo, Snapshot } from '../types'

const MARKER = 'playground.json'
const RESERVED = new Set(['templates', '_template'])

type Config = {
  name?: string
  agents_dir?: string
  runs_dir?: string
  catalog_dir?: string
}

export const EMPTY: Snapshot = {
  root: '',
  name: '',
  here: '',
  agents: [],
  runs: [],
  catalog: [],
  scannedAt: 0,
}

const sepOf = (path: string): string => (path.includes('\\') && !path.includes('/') ? '\\' : '/')

export const join = (base: string, ...parts: string[]): string => {
  const sep = sepOf(base)
  return [base.replace(/[\\/]+$/, ''), ...parts].join(sep)
}

const parentOf = (path: string): string | undefined => {
  const trimmed = path.replace(/[\\/]+$/, '')
  const cut = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'))
  if (cut <= 0) return cut === 0 && trimmed.length > 1 ? trimmed.slice(0, 1) : undefined
  const parent = trimmed.slice(0, cut)
  // Stop at a drive root like "D:".
  return /^[A-Za-z]:$/.test(parent) ? `${parent}\\` : parent
}

async function readJson<T>($: EngineInterface, path: string): Promise<T | undefined> {
  try {
    return JSON.parse(await $.fs.read(path)) as T
  } catch {
    return undefined
  }
}

const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : []

const text = (value: unknown): string => (typeof value === 'string' ? value : '')

async function listNames(
  $: EngineInterface,
  path: string,
  pick: (entry: { name: string; kind: string }) => string | undefined,
): Promise<string[]> {
  try {
    const entries = await $.fs.list(path)
    return entries
      .map(pick)
      .filter((n): n is string => n !== undefined && !n.startsWith('.'))
      .sort()
  } catch {
    return []
  }
}

/** The nearest folder at or above `cwd` that holds playground.json. */
export async function findRoot($: EngineInterface, cwd: string): Promise<string | undefined> {
  let folder: string | undefined = cwd
  for (let depth = 0; folder !== undefined && depth < 40; depth += 1) {
    if (await $.fs.exists(join(folder, MARKER))) return folder
    const up = parentOf(folder)
    folder = up === folder ? undefined : up
  }
  return undefined
}

async function scanAgent($: EngineInterface, folder: string, name: string): Promise<AgentInfo> {
  const meta = (await readJson<Record<string, unknown>>($, join(folder, 'agent.json'))) ?? {}
  const mcp = (await readJson<{ mcpServers?: Record<string, unknown> }>($, join(folder, '.mcp.json'))) ?? {}
  const claude = join(folder, '.claude')
  return {
    name,
    description: text(meta.description),
    tags: strings(meta.tags),
    connectors: strings(meta.connectors),
    skills: await listNames($, join(claude, 'skills'), e => (e.kind === 'dir' ? e.name : undefined)),
    subagents: await listNames($, join(claude, 'agents'), e =>
      e.kind === 'file' && e.name.endsWith('.md') ? e.name.slice(0, -3) : undefined,
    ),
    mcpServers: Object.keys(mcp.mcpServers ?? {}).sort(),
    tools: await listNames($, join(folder, 'tools'), e => e.name),
  }
}

export async function scanAgents($: EngineInterface, root: string, config: Config): Promise<AgentInfo[]> {
  const base = join(root, config.agents_dir ?? 'agents')
  const names = await listNames($, base, e => (e.kind === 'dir' && !RESERVED.has(e.name) ? e.name : undefined))
  const agents: AgentInfo[] = []
  for (const name of names) {
    const folder = join(base, name)
    if (await $.fs.exists(join(folder, 'CLAUDE.md'))) agents.push(await scanAgent($, folder, name))
  }
  return agents
}

export async function scanRuns($: EngineInterface, root: string, config: Config, limit = 15): Promise<RunInfo[]> {
  const base = join(root, config.runs_dir ?? '.playground/runs')
  const ids = (await listNames($, base, e => (e.kind === 'dir' ? e.name : undefined))).reverse().slice(0, limit)
  const runs: RunInfo[] = []
  for (const id of ids) {
    const run = await readJson<Record<string, unknown>>($, join(base, id, 'run.json'))
    if (!run) continue
    runs.push({
      id,
      agent: text(run.agent),
      task: text(run.task),
      status: text(run.status) || 'unknown',
      activity: text(run.activity),
      startedAt: text(run.started_at),
      updatedAt: text(run.updated_at),
      finishedAt: text(run.finished_at),
      error: text(run.error),
    })
  }
  return runs
}

export async function scanCatalog($: EngineInterface, root: string, config: Config): Promise<string[]> {
  return listNames($, join(root, config.catalog_dir ?? 'catalog', 'mcp'), e =>
    e.kind === 'file' && e.name.endsWith('.json') ? e.name.slice(0, -5) : undefined,
  )
}

/** Reads the whole workspace; `previous` supplies agents when only runs are rescanned. */
export async function scan(
  $: EngineInterface,
  cwd: string,
  options: { withAgents: boolean; previous: Snapshot },
): Promise<Snapshot> {
  const root = await findRoot($, cwd)
  if (root === undefined) return { ...EMPTY, scannedAt: await $.clock.now() }
  const config = (await readJson<Config>($, join(root, MARKER))) ?? {}
  const sameRoot = options.previous.root === root
  const agents = options.withAgents || !sameRoot ? await scanAgents($, root, config) : options.previous.agents
  const catalog = options.withAgents || !sameRoot ? await scanCatalog($, root, config) : options.previous.catalog
  const agentsBase = join(root, config.agents_dir ?? 'agents')
  const rest = cwd.startsWith(agentsBase) ? cwd.slice(agentsBase.length).replace(/^[\\/]+/, '') : ''
  const here = rest.split(/[\\/]/)[0] ?? ''
  return {
    root,
    name: config.name ?? 'Agent Playground',
    here: agents.some(a => a.name === here) ? here : '',
    agents,
    runs: await scanRuns($, root, config),
    catalog,
    scannedAt: await $.clock.now(),
  }
}

export const isActive = (run: RunInfo): boolean => run.status === 'running' || run.status === 'queued'

/** "3m", "2h", "5d" since an ISO time, or '' when it can't be read. */
export const ago = (iso: string, nowMs: number): string => {
  const at = Date.parse(iso)
  if (Number.isNaN(at)) return ''
  const s = Math.max(0, Math.round((nowMs - at) / 1000))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h`
  return `${Math.floor(s / 86400)}d`
}
