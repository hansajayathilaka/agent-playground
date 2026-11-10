import { expect, test } from 'claude-code/testing'

// A workspace held in memory: path -> file text. Folders are implied.
const FILES: Record<string, string> = {
  '/ws/playground.json': JSON.stringify({ name: 'Test Playground' }),
  '/ws/agents/media-editor/CLAUDE.md': '# media-editor',
  '/ws/agents/media-editor/agent.json': JSON.stringify({
    description: 'Edits photos and videos',
    connectors: ['Google Drive'],
  }),
  '/ws/agents/media-editor/.mcp.json': JSON.stringify({ mcpServers: { 'media-tools': { command: 'uv' } } }),
  '/ws/agents/media-editor/.claude/skills/resize-for-web/SKILL.md': '---\nname: resize-for-web\n---',
  '/ws/agents/media-editor/tools/watermark/README.md': 'x',
  '/ws/agents/templates/CLAUDE.md': 'not an agent',
  '/ws/catalog/mcp/media-tools.json': '{}',
  '/ws/.playground/runs/20261110-100000-media-editor-aaaa/run.json': JSON.stringify({
    agent: 'media-editor',
    task: 'Resize the photos in workspace/in',
    status: 'running',
    activity: 'image_resize workspace/in/a.jpg',
    started_at: '2026-11-10T10:00:00+00:00',
  }),
  '/ws/.playground/runs/20261110-090000-media-editor-bbbb/run.json': JSON.stringify({
    agent: 'media-editor',
    task: 'Trim the clip',
    status: 'failed',
    error: 'ffmpeg is not on PATH',
    started_at: '2026-11-10T09:00:00+00:00',
    finished_at: '2026-11-10T09:01:00+00:00',
  }),
}

const isDir = (path: string) => Object.keys(FILES).some(f => f.startsWith(`${path}/`))

const listDir = (path: string) => {
  const names = new Map<string, 'file' | 'dir'>()
  for (const file of Object.keys(FILES)) {
    if (!file.startsWith(`${path}/`)) continue
    const rest = file.slice(path.length + 1)
    const [head, ...tail] = rest.split('/')
    if (head) names.set(head, tail.length ? 'dir' : 'file')
  }
  return [...names].map(([name, kind]) => ({ name, kind, size: 0, mtimeMs: 0, isLink: false }))
}

const PANE = {
  plugin: 'playground-hud',
  component: 'Pane',
  requestId: 'playground',
  props: {
    title: 'Playground',
    isFocused: false,
    bodyColumns: 60,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 40 },
    view: {},
  },
} as const

const wire = (on: Parameters<Parameters<typeof test>[1]>[1], cwd: string) => {
  on('session.cwd', () => ({ value: cwd }))
  on('fs.exists', (_, e) => ({ value: e.path in FILES || isDir(e.path) }))
  on('fs.read', (_, e) => {
    const text = FILES[e.path]
    if (text === undefined) throw new Error(`ENOENT ${e.path}`)
    return { value: text }
  })
  on('fs.list', (_, e) => {
    if (!isDir(e.path)) throw new Error(`ENOENT ${e.path}`)
    return { value: listDir(e.path) }
  })
  on('clock.now', () => ({ value: Date.parse('2026-11-10T10:05:00Z') }))
  on('clock.every', () => ({ value: undefined }))
  on('command.register', () => ({ value: { command: 'playground' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
}

test('the pane shows agents, capabilities and runs on every surface', async ($, on) => {
  wire(on, '/ws/agents/media-editor')
  await $.session.start({ cwd: '/ws/agents/media-editor', surface: 'desktop', isInteractive: true })

  for (const surface of ['terminal', 'desktop', 'vscode', 'mobile'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /Test Playground/ }))?.text).toBe('Test Playground')
    expect(await ui.find({ type: 'Text', text: /This session is agent "media-editor"/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Ongoing \(1\)/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /image_resize/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /ffmpeg is not on PATH/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Agents \(1\)/ })).toBeDefined()
    const caps = await ui.find({ type: 'Text', text: /skills: resize-for-web/ })
    expect(caps?.text).toContain('mcp: media-tools')
    expect(caps?.text).toContain('tools: watermark')
    expect(caps?.text).toContain('connectors: Google Drive')
    expect(await ui.find({ key: 'run-media-editor' })).toBeDefined()
    await ui.unmount()
  }
})

test('outside a workspace the pane says so', async ($, on) => {
  wire(on, '/elsewhere')
  await $.session.start({ cwd: '/elsewhere', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await ui.find({ type: 'Text', text: /Not inside an Agent Playground workspace/ })).toBeDefined()
  await ui.unmount()
})

test('Run task fills the prompt with the delegate command', async ($, on) => {
  wire(on, '/ws')
  const filled: string[] = []
  on('prompt.fill', (_, e) => {
    filled.push(e.text)
    return { isFilled: true }
  })
  await $.session.start({ cwd: '/ws', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  await ui.press({ key: 'run-media-editor' })
  expect(filled).toEqual(['/playground:delegate media-editor '])
  await ui.unmount()
})
