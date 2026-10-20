#!/usr/bin/env node
/**
 * Integration verification against the **real** DSH seating.
 *
 * `verify.mjs` proves the providers work behind a fake `ctx.web`. This script
 * proves the other half: the plugin mounts inside cordis next to the real
 * `@deepseek-ai/dsh-web` service, the service's own provider selection resolves
 * `searchProvider` / `fetchProvider` to `llmmcp`, and `ctx.web.search()` /
 * `ctx.web.fetch()` — the exact calls `dsh-tool-web` makes — return data.
 *
 * Everything is resolved from the DSH profile, so this is the same code the
 * harness loads:
 *
 *   node scripts/verify-seam.mjs [--profile desktop] [--query "..."] [--url ...]
 */

import { createRequire } from 'node:module'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import process from 'node:process'

const here = fileURLToPath(new URL('.', import.meta.url))
const packageDir = resolve(here, '..')
const pluginEntry = join(packageDir, 'index.js')
const worktreeRoot = resolve(packageDir, '..', '..')
const defaultPython = join(worktreeRoot, '.venv', 'Scripts', 'python.exe')

function parseArgs(argv) {
  const options = {
    home: process.env.DSH_HOME ?? join(homedir(), '.dsh'),
    profile: 'desktop',
    query: 'ascend npu mfu',
    maxResults: 5,
    url: 'https://example.com',
    command: undefined,
    cwd: worktreeRoot,
  }
  for (let index = 0; index < argv.length; index += 1) {
    const next = () => argv[++index]
    switch (argv[index]) {
      case '--home': options.home = next(); break
      case '--profile': options.profile = next(); break
      case '--query': options.query = next(); break
      case '--max-results': options.maxResults = Number(next()); break
      case '--url': options.url = next(); break
      case '--command': options.command = next(); break
      case '--cwd': options.cwd = next(); break
      case '--help': case '-h':
        process.stdout.write('usage: node scripts/verify-seam.mjs [--profile desktop] [--query text] [--url url]\n')
        process.exit(0)
        break
      default: throw new Error(`verify-seam: unknown option ${argv[index]}`)
    }
  }
  return options
}

const options = parseArgs(process.argv.slice(2))
const profileDir = join(options.home, 'profiles', options.profile)
const anchor = join(profileDir, 'package.json')
if (!existsSync(anchor)) {
  process.stdout.write(`result: FAILED — ${anchor} is missing\n`)
  process.exit(1)
}

const require = createRequire(anchor)
const cordis = await import(pathToFileURL(require.resolve('@deepseek-ai/cordis')).href)
const webModule = await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-web')).href)
const WebRuntime = webModule.default ?? webModule.WebRuntime
const pluginModule = await import(pathToFileURL(pluginEntry).href)

const command = options.command ?? defaultPython
process.stdout.write(`profile:   ${profileDir}\n`)
process.stdout.write(`plugin:    ${pluginEntry}\n`)
process.stdout.write(`command:   ${command} -m llmmcp\n\n`)

const ctx = new cordis.Context()
const webFiber = ctx.plugin(WebRuntime, { searchProvider: 'llmmcp', fetchProvider: 'llmmcp' })
await webFiber.await()
if (!ctx.web) throw new Error('verify-seam: ctx.web did not appear after mounting WebRuntime')
process.stdout.write('mounted:   @deepseek-ai/dsh-web with searchProvider=fetchProvider=llmmcp\n')

const pluginFiber = ctx.plugin(pluginModule, {
  providerId: 'llmmcp',
  command,
  args: ['-m', 'llmmcp'],
  cwd: options.cwd,
  warmup: true,
})
await pluginFiber.await()
process.stdout.write('mounted:   llm-dsh-web-mcp (fiber active)\n\n')

let failures = 0
const started = Date.now()
try {
  const search = await ctx.web.search({ query: options.query, maxResults: options.maxResults })
  const first = search.sources[0]
  process.stdout.write(`ctx.web.search(${JSON.stringify(options.query)}, maxResults=${options.maxResults})\n`)
  process.stdout.write(`  sources:   ${search.sources.length}\n`)
  process.stdout.write(`  truncated: ${search.truncated}\n`)
  process.stdout.write(`  first:     ${first ? `${first.title ?? '(no title)'} — ${first.url}` : '(none)'}\n`)
  process.stdout.write(`  elapsed:   ${Date.now() - started} ms\n`)
  if (!first) failures += 1

  const fetchStarted = Date.now()
  const fetched = await ctx.web.fetch({ url: options.url })
  process.stdout.write(`\nctx.web.fetch(${JSON.stringify(options.url)})\n`)
  process.stdout.write(`  url:       ${fetched.url}\n`)
  process.stdout.write(`  status:    ${fetched.statusCode}\n`)
  process.stdout.write(`  body.kind: ${fetched.body.kind}\n`)
  process.stdout.write(`  body len:  ${fetched.body.content.length}\n`)
  process.stdout.write(`  truncated: ${fetched.truncated}\n`)
  process.stdout.write(`  elapsed:   ${Date.now() - fetchStarted} ms\n`)
  if (!(fetched.statusCode >= 200 && fetched.statusCode < 300) || fetched.body.content.length === 0) failures += 1
} catch (error) {
  failures += 1
  process.stdout.write(`\nerror: ${error && error.stack ? error.stack : String(error)}\n`)
} finally {
  await pluginFiber.dispose()
  await webFiber.dispose()
}

process.stdout.write(`\nresult: ${failures === 0 ? 'OK' : 'FAILED'}\n`)
process.exit(failures === 0 ? 0 : 1)
