import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { AgentInfo, RunInfo, Snapshot } from '../types'
import { EMPTY, ago, isActive, scan } from './scan'

const PANE = 'playground'
const TITLE = 'Playground'
const RUNS_EVERY_MS = 3_000
const AGENTS_EVERY = 5 // rescan agents every 5th tick (15 s)

// Bump the shape tag when Snapshot's fields change: it drops what an older
// version stored, which outlives a reload.
const snapshot = atom({ plugin: 'playground-hud', key: 'snapshot' } as const, EMPTY, {
  shape: 'snapshot-1',
})

const STATUS_MARK: Record<string, string> = {
  running: '●',
  queued: '○',
  done: '✓',
  failed: '✗',
  lost: '?',
}

const list = (label: string, items: string[]): string => (items.length ? `${label} ${items.join(', ')}` : '')

const capabilities = (agent: AgentInfo): string =>
  [
    list('skills:', agent.skills),
    list('subagents:', agent.subagents),
    list('mcp:', agent.mcpServers),
    list('tools:', agent.tools),
    list('connectors:', agent.connectors),
  ]
    .filter(Boolean)
    .join(' · ') || 'no skills, MCP servers or tools yet'

const clip = (value: string, max: number): string => (value.length > max ? `${value.slice(0, max - 1)}…` : value)

// The session's directory, read once at session start (timers have no hook of their own).
let sessionCwd = ''
let tick = 0

async function refresh($: EngineInterface, withAgents: boolean): Promise<void> {
  const previous = await read($, snapshot)
  const next = await scan($, sessionCwd, { withAgents, previous })
  const same = JSON.stringify({ ...next, scannedAt: 0 }) === JSON.stringify({ ...previous, scannedAt: 0 })
  if (same) return
  await update($, snapshot, () => next)
  // Toast each run that finished since the last scan.
  const before = new Map(previous.runs.map(run => [run.id, run]))
  for (const run of next.runs) {
    const was = before.get(run.id)
    if (was !== undefined && isActive(was) && !isActive(run)) {
      $.ui.toast(`${run.agent}: run ${run.status}`)
    }
  }
}

async function fill($: EngineInterface, text: string): Promise<void> {
  await $.prompt.fill({ text })
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    sessionCwd = await $.session.cwd()
    tick = 0
    await $.command.register({
      name: 'playground',
      description: 'Open the Agent Playground pane: agents, their tools and ongoing runs',
    })
    await refresh($, true)
    $.clock.every(RUNS_EVERY_MS, () => {
      tick += 1
      void refresh($, tick % AGENTS_EVERY === 0)
    })
    const now = await read($, snapshot)
    if (now.root !== '') void $.ui.open({ id: PANE, title: TITLE })

    return next(e)
  })

  on('command.run', { command: 'playground' }, async $ => {
    await refresh($, true)
    const now = await read($, snapshot)
    if (now.root === '') {
      return { text: 'Not inside an Agent Playground workspace (no playground.json above this folder).' }
    }
    const placed = await $.ui.open({ id: PANE, title: TITLE })
    const active = now.runs.filter(isActive).length
    const summary = `${now.agents.length} agents, ${active} running.`

    return { text: placed.isPlaced ? `Playground pane opened. ${summary}` : summary }
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const now: Snapshot = await read($, snapshot)
    const active = now.runs.filter(isActive)
    if (now.root === '' || active.length === 0 || e.props.hasSurvey) return next(e)

    const { Box, Button, Text } = $.ui.resolve(e)
    const first = active[0]

    return (
      <Box>
        <Text color="cyan">● </Text>
        <Text>
          {active.length === 1 ? '1 agent run' : `${active.length} agent runs`} ongoing
        </Text>
        {first !== undefined && (
          <Text dimColor>
            {' '}
            {first.agent}: {clip(first.activity || first.task, 60)}{' '}
          </Text>
        )}
        <Button key="open-pane" label="Open" onPress={() => void $.ui.open({ id: PANE, title: TITLE })} />
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    const now: Snapshot = await read($, snapshot)
    const nowMs = await $.clock.now()

    if (now.root === '') {
      return (
        <Box flexDirection="column">
          <Text dimColor>Not inside an Agent Playground workspace.</Text>
          <Text dimColor>Start Claude in the workspace root or an agent folder.</Text>
        </Box>
      )
    }

    const active = now.runs.filter(isActive)
    const recent = now.runs.filter(run => !isActive(run)).slice(0, 6)
    const runRow = (run: RunInfo) => (
      <Box key={`run-${run.id}`} flexDirection="column">
        <Box>
          <Text color={run.status === 'failed' ? 'red' : run.status === 'done' ? 'green' : 'cyan'}>
            {STATUS_MARK[run.status] ?? '·'}{' '}
          </Text>
          <Text bold>{run.agent} </Text>
          <Text dimColor>{ago(run.finishedAt || run.startedAt, nowMs)} </Text>
          <Button
            key={`show-${run.id}`}
            label="Show"
            onPress={() => fill($, `/playground:delegate result ${run.id}`)}
          />
        </Box>
        <Text dimColor>
          {'  '}
          {clip(isActive(run) ? run.activity || run.task : run.error || run.task, 70)}
        </Text>
      </Box>
    )

    return (
      <Box flexDirection="column">
        <Text bold>{now.name}</Text>
        {now.here !== '' && <Text dimColor>This session is agent "{now.here}"</Text>}

        <Text> </Text>
        <Text bold>Ongoing ({active.length})</Text>
        {active.length === 0 && <Text dimColor>Nothing running.</Text>}
        {active.map(runRow)}

        {recent.length > 0 && <Text> </Text>}
        {recent.length > 0 && <Text bold>Recent</Text>}
        {recent.map(runRow)}

        <Text> </Text>
        <Text bold>Agents ({now.agents.length})</Text>
        {now.agents.length === 0 && <Text dimColor>No agents yet.</Text>}
        {now.agents.map(agent => (
          <Box key={`agent-${agent.name}`} flexDirection="column">
            <Box>
              <Text bold color={agent.name === now.here ? 'cyan' : undefined}>
                {agent.name}{' '}
              </Text>
              <Button
                key={`run-${agent.name}`}
                label="Run task"
                onPress={() => fill($, `/playground:delegate ${agent.name} `)}
              />
              <Text> </Text>
              <Button
                key={`edit-${agent.name}`}
                label="Edit"
                onPress={() => fill($, `/playground:build-agent edit ${agent.name} `)}
              />
            </Box>
            {agent.description !== '' && <Text>{'  '}{clip(agent.description, 80)}</Text>}
            <Text dimColor>{'  '}{capabilities(agent)}</Text>
          </Box>
        ))}

        <Text> </Text>
        <Text dimColor>Catalog: {now.catalog.length ? now.catalog.join(', ') : 'empty'}</Text>
        <Box>
          <Button key="new-agent" label="New agent" onPress={() => fill($, '/playground:build-agent ')} />
          <Text> </Text>
          <Button key="add-mcp" label="Add MCP" onPress={() => fill($, '/playground:add-mcp ')} />
          <Text> </Text>
          <Button key="refresh" label="Refresh" onPress={() => refresh($, true)} />
        </Box>
      </Box>
    )
  })
}
