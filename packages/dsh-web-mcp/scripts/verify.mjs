#!/usr/bin/env node
/**
 * Standalone verification for the `llm-dsh-web-mcp` bridge.
 *
 * Loads `index.js` with a fake cordis context (a `ctx.web` that only records
 * `registerSearchProvider` / `registerFetchProvider`, a callable `ctx.logger`,
 * and an `ctx.effect` that collects disposers), then drives the registered
 * providers through a **real** MCP child process:
 *
 *   node scripts/verify.mjs                 # real Python LLMMcp server
 *   node scripts/verify.mjs --mock          # bundled fake stdio server
 *   node scripts/verify.mjs --checks        # plus error/timeout/reuse probes
 *
 * Expected output: the search source count and first hit, the fetch status code
 * and body length, and the wall-clock cost of each. Exit code 0 means both
 * providers returned usable data.
 */

import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, resolve } from 'node:path'
import process from 'node:process'

const here = dirname(fileURLToPath(import.meta.url))
const packageDir = resolve(here, '..')
const pluginEntry = resolve(packageDir, 'index.js')
const fakeServer = resolve(here, 'fake-mcp-server.mjs')

/** The worktree this package lives in (…/packages/dsh-web-mcp → …). */
const worktreeRoot = resolve(packageDir, '..', '..')
const defaultPython = resolve(worktreeRoot, '.venv', 'Scripts', 'python.exe')

const USAGE = `usage: node scripts/verify.mjs [options]

  --mock               use the bundled fake MCP server instead of Python
  --checks             also probe error, timeout and connection-reuse paths
  --verbose            print the bridge's own logger lines
  --query <text>       search query            (default: "ascend npu mfu")
  --max-results <n>    maxResults sent to ctx.web (default: 5)
  --url <url>          URL to fetch            (default: https://example.com)
  --command <path>     MCP server executable    (default: <worktree>/.venv/Scripts/python.exe)
  --cwd <dir>          child working directory (default: <worktree>)
  --timeout <ms>       per-call budget          (default: 120000)
`

function parseArgs(argv) {
  const options = {
    mock: false,
    checks: false,
    verbose: false,
    query: 'ascend npu mfu',
    maxResults: 5,
    url: 'https://example.com',
    command: undefined,
    cwd: undefined,
    timeout: 120000,
  }
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    const value = () => {
      index += 1
      if (index >= argv.length) throw new Error(`verify: ${arg} needs a value`)
      return argv[index]
    }
    switch (arg) {
      case '--mock': options.mock = true; break
      case '--checks': options.checks = true; break
      case '--verbose': options.verbose = true; break
      case '--query': options.query = value(); break
      case '--max-results': options.maxResults = Number(value()); break
      case '--url': options.url = value(); break
      case '--command': options.command = value(); break
      case '--cwd': options.cwd = value(); break
      case '--timeout': options.timeout = Number(value()); break
      case '--help': case '-h': process.stdout.write(USAGE); process.exit(0); break
      default: throw new Error(`verify: unknown option ${arg}`)
    }
  }
  return options
}

/**
 * A fake cordis context: the smallest surface the plugin touches.
 * @param {boolean} verbose - echo logger lines to stdout.
 */
function createFakeContext(verbose) {
  const lines = []
  const record = level => (...args) => {
    const message = args.map(item => (typeof item === 'string' ? item : String(item))).join(' ')
    lines.push({ level, message })
    if (verbose) process.stdout.write(`    [${level}] ${message}\n`)
  }
  const logger = Object.assign(() => logger, {
    debug: record('debug'),
    info: record('info'),
    warn: record('warn'),
    error: record('error'),
  })
  const registrations = { search: [], fetch: [] }
  const disposers = []
  const web = {
    registerSearchProvider(provider) {
      if (registrations.search.some(item => item.id === provider.id)) {
        throw new Error(`duplicate search provider id ${provider.id}`)
      }
      registrations.search.push(provider)
      const dispose = () => { registrations.search = registrations.search.filter(item => item !== provider) }
      return dispose
    },
    registerFetchProvider(provider) {
      if (registrations.fetch.some(item => item.id === provider.id)) {
        throw new Error(`duplicate fetch provider id ${provider.id}`)
      }
      registrations.fetch.push(provider)
      const dispose = () => { registrations.fetch = registrations.fetch.filter(item => item !== provider) }
      return dispose
    },
  }
  return {
    web,
    logger,
    registrations,
    lines,
    effect(execute) {
      const disposer = execute()
      if (typeof disposer === 'function') disposers.push(disposer)
      return () => {}
    },
    async dispose() {
      for (const disposer of disposers.reverse()) {
        try {
          await disposer()
        } catch (error) {
          process.stdout.write(`    dispose error: ${error.message}\n`)
        }
      }
    },
  }
}

const startedAt = new Map()

/** Time an async probe and print its duration. */
async function timed(label, task) {
  const started = startedAt.set(label, Date.now())
  const value = await task()
  return { value, elapsed: Date.now() - startedAt.get(label) }
}

async function main() {
  const options = parseArgs(process.argv.slice(2))
  const config = options.mock
    ? { command: process.execPath, args: [fakeServer], cwd: packageDir, providerId: 'llmmcp', warmup: true }
    : {
        command: options.command ?? defaultPython,
        args: ['-m', 'llmmcp'],
        cwd: options.cwd ?? worktreeRoot,
        providerId: 'llmmcp',
        warmup: true,
      }
  config.toolCallTimeoutMs = options.timeout

  process.stdout.write(`mode:        ${options.mock ? 'mock MCP server' : 'real LLMMcp (python -m llmmcp)'}\n`)
  process.stdout.write(`command:     ${config.command} ${(config.args ?? []).join(' ')}\n`)
  process.stdout.write(`cwd:         ${config.cwd}\n`)
  process.stdout.write(`plugin:      ${pluginEntry}\n\n`)

  const module = await import(pathToFileURL(pluginEntry).href)
  const ctx = createFakeContext(options.verbose)
  const loadStarted = Date.now()
  module.apply(ctx, config)
  process.stdout.write(`apply():     ${Date.now() - loadStarted} ms\n`)
  process.stdout.write(`registered:  search=[${ctx.registrations.search.map(item => item.id).join(', ')}] fetch=[${ctx.registrations.fetch.map(item => item.id).join(', ')}]\n`)

  const searchProvider = ctx.registrations.search[0]
  const fetchProvider = ctx.registrations.fetch[0]
  if (!searchProvider || !fetchProvider) throw new Error('verify: the plugin registered no providers')

  process.stdout.write(`available(): search=${searchProvider.available()} fetch=${fetchProvider.available()}\n`)
  const effective = module.resolveOptions(config)
  process.stdout.write(`timeouts:   search=${effective.searchTimeoutS}s fetch=${effective.fetchTimeoutS}s call budget=${effective.toolCallTimeoutMs}ms\n\n`)

  let failures = 0

  // ── search ────────────────────────────────────────────────────────────────
  const searchTimeout = AbortSignal.timeout(options.timeout)
  const searchProbe = await timed('search', () => searchProvider.search({ query: options.query, maxResults: options.maxResults }, searchTimeout))
  const searchResult = searchProbe.value
  const firstSource = searchResult.sources[0]
  process.stdout.write(`search(${JSON.stringify(options.query)}, ${options.maxResults})\n`)
  process.stdout.write(`  sources:   ${searchResult.sources.length}\n`)
  process.stdout.write(`  truncated: ${searchResult.truncated}\n`)
  process.stdout.write(`  first:     ${firstSource ? `${firstSource.title ?? '(no title)'} — ${firstSource.url}` : '(no sources)'}\n`)
  if (firstSource?.snippet) process.stdout.write(`  snippet:   ${firstSource.snippet.slice(0, 120)}\n`)
  process.stdout.write(`  elapsed:   ${searchProbe.elapsed} ms\n`)
  if (!firstSource || searchResult.sources.length === 0) failures += 1

  // ── fetch ─────────────────────────────────────────────────────────────────
  const fetchTimeout = AbortSignal.timeout(options.timeout)
  const fetchProbe = await timed('fetch', () => fetchProvider.fetch({ url: options.url }, fetchTimeout))
  const fetchResult = fetchProbe.value
  process.stdout.write(`\nfetch(${JSON.stringify(options.url)})\n`)
  process.stdout.write(`  url:       ${fetchResult.url}\n`)
  process.stdout.write(`  status:    ${fetchResult.statusCode}\n`)
  process.stdout.write(`  body.kind: ${fetchResult.body.kind}\n`)
  process.stdout.write(`  body len:  ${fetchResult.body.content.length}\n`)
  process.stdout.write(`  truncated: ${fetchResult.truncated}\n`)
  process.stdout.write(`  elapsed:   ${fetchProbe.elapsed} ms\n`)
  if (!(fetchResult.statusCode >= 200 && fetchResult.statusCode < 300) || fetchResult.body.content.length === 0) failures += 1

  // ── second call reuses the same child ─────────────────────────────────────
  const reuseProbe = await timed('reuse', () => fetchProvider.fetch({ url: options.url }, AbortSignal.timeout(options.timeout)))
  process.stdout.write(`\nsecond fetch (connection reuse): ${reuseProbe.elapsed} ms, body len ${reuseProbe.value.body.content.length}\n`)

  // ── optional error/timeout probes ─────────────────────────────────────────
  if (options.checks) {
    process.stdout.write('\nchecks:\n')
    const { McpStdioClient } = module
    const failing = new McpStdioClient({ ...module.resolveOptions(config), env: module.buildChildEnv({}), logger: ctx.logger })
    try {
      await failing.callTool('web_does_not_exist', {}, { timeoutMs: 15000 })
      process.stdout.write('  unknown-tool: FAILED (no error)\n')
      failures += 1
    } catch (error) {
      const ok = /unknown tool|method not found|-32602|-32601/i.test(error.message)
      process.stdout.write(`  unknown-tool: ${ok ? 'ok' : 'FAILED'} → ${error.message.slice(0, 180)}\n`)
      if (!ok) failures += 1
    }
    const slow = new McpStdioClient({ ...module.resolveOptions({ ...config, toolCallTimeoutMs: 250 }), env: module.buildChildEnv({}), logger: ctx.logger })
    try {
      await slow.callTool(options.mock ? 'web_slow' : 'web_search', options.mock ? {} : { query: options.query }, { timeoutMs: 250 })
      process.stdout.write('  timeout: FAILED (returned instead of timing out)\n')
      failures += 1
    } catch (error) {
      const ok = /timed out after 250 ms/.test(error.message)
      process.stdout.write(`  timeout: ${ok ? 'ok' : 'FAILED'} → ${error.message.split('\n')[0]}\n`)
      if (!ok) failures += 1
    }
    await failing.close()
    await slow.close()

    if (options.mock) {
      const abort = new AbortController()
      const pending = fetchProvider.fetch({ url: options.url }, abort.signal).then(
        () => 'resolved',
        error => error.name,
      )
      abort.abort()
      const outcome = await pending
      process.stdout.write(`  cancellation: ${outcome === 'AbortError' ? 'ok' : `FAILED (${outcome})`}\n`)
      if (outcome !== 'AbortError') failures += 1

      const errorText = await searchProvider.search({ query: '__error__', maxResults: 3 }).then(
        () => 'resolved',
        error => error.message,
      )
      const ok = /SEARCH_BLOCKED/.test(errorText) && /mock engine refused/.test(errorText)
      process.stdout.write(`  tool error: ${ok ? 'ok' : 'FAILED'} → ${errorText.slice(0, 180)}\n`)
      if (!ok) failures += 1
    }
  }

  await ctx.dispose()
  process.stdout.write(`\nresult: ${failures === 0 ? 'OK' : `FAILED (${failures} problem(s))`}\n`)
  return failures === 0 ? 0 : 1
}

const exitCode = await main().then(
  code => code,
  (error) => {
    process.stdout.write(`\nresult: FAILED\n${error && error.stack ? error.stack : String(error)}\n`)
    return 1
  },
)
// The MCP child is disposed above; exit explicitly so a stray handle cannot hang CI.
process.exit(exitCode)
