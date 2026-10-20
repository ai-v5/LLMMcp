/**
 * `web-mcp`  - a cordis plugin for DeepSeek Harness that answers the `ctx.web`
 * seam (search + fetch) from the local **LLMMcp** MCP server over stdio.
 *
 * Three pieces live here on purpose, so the package stays loadable from a DSH
 * profile with zero installed dependencies:
 *
 *   1. {@link McpStdioClient}  - a minimal MCP client (newline-delimited JSON-RPC
 *      2.0): `initialize` 鈫?`notifications/initialized` 鈫?`tools/call`, with id
 *      matching, per-call timeouts, cancellation, and respawn after an exit.
 *   2. {@link LLMMcpSearchProvider}  - MCP `web_search` 鈫?`WebSearchResult`.
 *   3. {@link LLMMcpFetchProvider}  - MCP `web_fetch` 鈫?`WebFetchResult`.
 *
 * Invariants this file keeps:
 *    - stdout of the host belongs to nobody. Every diagnostic goes through
 *     `ctx.logger`; the child's stdout is the JSON-RPC channel and its stderr is
 *     drained into the logger (credential-shaped fragments redacted).
 *    - `available()` never touches the network: it only asks whether the
 *     configured command resolves, so provider selection stays synchronous.
 *    - Nothing is imported at module scope except Node builtins.
 *
 * @module llm-dsh-web-mcp
 */

import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { delimiter, extname, isAbsolute, join } from 'node:path'

import { resolveRuntime } from './runtime.mjs'

/** Plugin name used by loader diagnostics. */
export const name = 'web-mcp'

/** The bridge serves `ctx.web`; cordis mounts it once that service is up. */
export const inject = ['web']

/** Largest single JSON-RPC frame accepted from the child before the buffer is dropped. */
const MAX_FRAME_CHARS = 64 * 1024 * 1024

/** Resolved plugin defaults; every field is a deployment knob (see README). */
export const DEFAULTS = Object.freeze({
  /** Provider id registered on `ctx.web`; set `searchProvider`/`fetchProvider` to it. */
  providerId: 'llmmcp',
  /** Executable that starts the MCP server on stdio. */
  command: '',
  /** Arguments for `command`; `['-m', 'llmmcp']` for the Python server. */
  args: [],
  /** Working directory for the child process. */
  cwd: undefined,
  /** Extra environment variables merged over the (secret-scrubbed) parent env. */
  env: {},
  /** MCP tool names. */
  searchTool: 'web_search',
  fetchTool: 'web_fetch',
  /** Per-`tools/call` budget (LLMMcp search is a rendered-page fetch: allow ~60s). */
  toolCallTimeoutMs: 60000,
  /**
   * Per-request timeout handed to `web_search` (seconds). Derived when unset:
   * `min(55, toolCallTimeoutMs/1000 - 5)`  - Google's first search renders a page
   * in a headless browser (8-20s, longer on a busy machine), so the tool default
   * of 25s is too tight.
   */
  searchTimeoutS: undefined,
  /** Per-request timeout handed to `web_fetch` (seconds). Derived: `min(40, toolCallTimeoutMs/1000 - 5)`. */
  fetchTimeoutS: undefined,
  /** Budget for spawn + `initialize` + `tools/list`. */
  initializeTimeoutMs: 30000,
  /** Start the child during plugin load instead of on the first call. */
  warmup: true,
  /**
   * `format` sent to `web_fetch`. Markdown is requested and then declared to the
   * seam as `text`: the server already owns boilerplate removal and conversion,
   * and `dsh-tool-web` falls back to its own turndown pass on `html`, which
   * drops heavily nested application markup. Converting once is both cheaper
   * and more predictable.
   */
  fetchFormat: 'markdown',
  /** `max_chars` sent to `web_fetch`; above `dsh-tool-web`'s 200k output cap. */
  fetchMaxChars: 400000,
  /** MCP protocol version requested at `initialize` (a legacy/handshake version). */
  protocolVersion: '2025-06-18',
  /** Client identity reported to the server. */
  clientName: 'dsh-web-mcp',
  clientVersion: '0.1.0',
  /** Characters of the child's stderr kept for error messages. */
  stderrTailChars: 4000,
  /** Install LLMMcp automatically when no configured command resolves. */
  autoInstall: true,
  /** Interpreter used to create the managed venv; empty probes `py`, `python`, `python3`. */
  python: '',
  /** Directory holding the machine-local config and the managed venv; empty means `~/.llmmcp`. */
  llmmcpHome: '',
  /** Managed venv directory; empty means `<llmmcpHome>/venv`. */
  venvDir: '',
  /** What to `pip install`; empty means the published release wheel. */
  installSpec: '',
  /** Budget for venv creation and `pip install` (ms). */
  installTimeoutMs: 900000,
})

// ---------------------------------------------------------------------------
// config
// ---------------------------------------------------------------------------

/**
 * Resolve and validate the plugin config. The plugin exports no schemastery
 * `Config` on purpose: importing `@deepseek-ai/schemastery` would add a module
 * the profile only resolves through the harness, and a resolution failure at
 * import time would drop the whole row. Validation therefore happens here and
 * throws a message that names the offending field.
 *
 * @param {Record<string, unknown> | undefined} config - the loader entry config.
 * @returns {typeof DEFAULTS} resolved options.
 */
export function resolveOptions(config) {
  const raw = config ?? {}
  const callBudgetMs = positiveIntegerOption(raw.toolCallTimeoutMs, DEFAULTS.toolCallTimeoutMs, 'toolCallTimeoutMs')
  // Keep every per-request timeout below the transport budget, so the server's
  // own timeout error (which names the failing engine) wins over a client abort.
  const headroomS = Math.max(5, Math.floor(callBudgetMs / 1000) - 5)
  return {
    providerId: stringOption(raw.providerId, DEFAULTS.providerId, 'providerId'),
    command: stringOption(raw.command, DEFAULTS.command, 'command'),
    args: stringListOption(raw.args, DEFAULTS.args, 'args'),
    cwd: optionalStringOption(raw.cwd, DEFAULTS.cwd, 'cwd'),
    env: recordOption(raw.env, DEFAULTS.env, 'env'),
    searchTool: stringOption(raw.searchTool, DEFAULTS.searchTool, 'searchTool'),
    fetchTool: stringOption(raw.fetchTool, DEFAULTS.fetchTool, 'fetchTool'),
    toolCallTimeoutMs: callBudgetMs,
    searchTimeoutS: positiveNumberOption(raw.searchTimeoutS, Math.min(55, headroomS), 'searchTimeoutS'),
    fetchTimeoutS: positiveNumberOption(raw.fetchTimeoutS, Math.min(40, headroomS), 'fetchTimeoutS'),
    initializeTimeoutMs: positiveIntegerOption(raw.initializeTimeoutMs, DEFAULTS.initializeTimeoutMs, 'initializeTimeoutMs'),
    warmup: booleanOption(raw.warmup, DEFAULTS.warmup, 'warmup'),
    fetchFormat: stringOption(raw.fetchFormat, DEFAULTS.fetchFormat, 'fetchFormat'),
    fetchMaxChars: positiveIntegerOption(raw.fetchMaxChars, DEFAULTS.fetchMaxChars, 'fetchMaxChars'),
    protocolVersion: stringOption(raw.protocolVersion, DEFAULTS.protocolVersion, 'protocolVersion'),
    clientName: stringOption(raw.clientName, DEFAULTS.clientName, 'clientName'),
    clientVersion: stringOption(raw.clientVersion, DEFAULTS.clientVersion, 'clientVersion'),
    stderrTailChars: positiveIntegerOption(raw.stderrTailChars, DEFAULTS.stderrTailChars, 'stderrTailChars'),
    autoInstall: booleanOption(raw.autoInstall, DEFAULTS.autoInstall, 'autoInstall'),
    python: stringOption(raw.python, DEFAULTS.python, 'python'),
    llmmcpHome: stringOption(raw.llmmcpHome, DEFAULTS.llmmcpHome, 'llmmcpHome'),
    venvDir: stringOption(raw.venvDir, DEFAULTS.venvDir, 'venvDir'),
    installSpec: stringOption(raw.installSpec, DEFAULTS.installSpec, 'installSpec'),
    installTimeoutMs: positiveIntegerOption(raw.installTimeoutMs, DEFAULTS.installTimeoutMs, 'installTimeoutMs'),
  }
}

function stringOption(value, fallback, field) {
  if (value === undefined || value === null) return fallback
  if (typeof value !== 'string') throw new Error(`web-mcp: config.${field} must be a string`)
  return value
}

function optionalStringOption(value, fallback, field) {
  if (value === undefined || value === null) return fallback
  if (typeof value !== 'string' || value.trim() === '') throw new Error(`web-mcp: config.${field} must be a non-empty string`)
  return value
}

function stringListOption(value, fallback, field) {
  if (value === undefined || value === null) return fallback
  if (!Array.isArray(value) || value.some(item => typeof item !== 'string')) {
    throw new Error(`web-mcp: config.${field} must be an array of strings`)
  }
  return [...value]
}

function recordOption(value, fallback, field) {
  if (value === undefined || value === null) return fallback
  if (typeof value !== 'object' || Array.isArray(value)) throw new Error(`web-mcp: config.${field} must be an object`)
  const out = {}
  for (const [key, item] of Object.entries(value)) out[key] = String(item)
  return out
}

function positiveIntegerOption(value, fallback, field) {
  if (value === undefined || value === null) return fallback
  const parsed = Number(value)
  if (!Number.isInteger(parsed) || parsed < 1) throw new Error(`web-mcp: config.${field} must be a positive integer`)
  return parsed
}

/** Optional positive (fractional allowed) number, e.g. seconds. */
function positiveNumberOption(value, fallback, field) {
  if (value === undefined || value === null) return fallback
  const parsed = Number(value)
  if (!Number.isFinite(parsed) || parsed <= 0) throw new Error(`web-mcp: config.${field} must be a positive number`)
  return parsed
}

function booleanOption(value, fallback, field) {
  if (value === undefined || value === null) return fallback
  if (typeof value !== 'boolean') throw new Error(`web-mcp: config.${field} must be a boolean`)
  return value
}

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------

/** Names the harness itself scrubs from child processes; keep the same shape. */
const SECRET_ENV_PATTERN = /KEY|PASSWORD|SECRET|TOKEN|CREDENTIAL|AUTH/i

/**
 * Child environment: the parent environment minus credential-shaped names (the
 * bridge never needs them), plus `config.env` (which always wins, because an
 * explicit override is a deliberate deployment decision), plus UTF-8 stdio.
 *
 * @param {Record<string, string>} extra - configured overrides.
 * @returns {Record<string, string>} the child environment.
 */
export function buildChildEnv(extra) {
  const env = {}
  for (const [key, value] of Object.entries(process.env)) {
    if (value === undefined) continue
    if (SECRET_ENV_PATTERN.test(key)) continue
    env[key] = value
  }
  Object.assign(env, extra)
  env.PYTHONUNBUFFERED = '1'
  env.PYTHONUTF8 = '1'
  env.PYTHONIOENCODING = 'utf-8'
  return env
}

/**
 * Redact `user:password@` userinfo from text that is about to be logged or
 * embedded in an error message. Proxy URLs are legitimate diagnostics; their
 * credentials are not.
 *
 * @param {string} text - raw text.
 * @returns {string} the same text with userinfo collapsed to `***`.
 */
export function redactCredentials(text) {
  if (typeof text !== 'string' || text === '') return text
  return text.replace(/([a-z][a-z0-9+.-]*:\/\/)[^\s/?#@]+@/gi, '$1***@')
}

/** Truncate for diagnostics, keeping both ends visible. */
function bound(text, limit) {
  if (typeof text !== 'string') return ''
  if (text.length <= limit) return text
  const head = text.slice(0, Math.floor(limit / 2))
  const tail = text.slice(text.length - Math.floor(limit / 2))
  return `${head} ... ${tail}`
}

/** Sleep helper used only by `close()`'s escalation ladder. */
function delay(ms) {
  return new Promise(resolve => {
    const timer = setTimeout(resolve, ms)
    if (typeof timer.unref === 'function') timer.unref()
  })
}

/** Whether `promise` settled (either way) within `ms`; never rejects. */
function settlesWithin(promise, ms) {
  const settled = Promise.resolve(promise).then(() => true, () => true)
  return Promise.race([settled, delay(ms).then(() => false)])
}

/**
 * Kill a child and its descendants. A venv/launcher shim can run the real
 * interpreter as its own child, so killing only the direct child would leave
 * the MCP server alive; on Windows `taskkill /T` takes the whole tree, and
 * elsewhere `child.kill()` is enough because the shim is `exec`-like.
 */
function killTree(child) {
  if (process.platform === 'win32' && typeof child?.pid === 'number') {
    try {
      const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
      killer.on('error', () => {})
      return
    } catch {}
  }
  try {
    child?.kill()
  } catch {}
}

/** A cancellation error recognisable by consumers (`name === 'AbortError'`). */
function abortError() {
  const error = new Error('web-mcp: the web request was aborted')
  error.name = 'AbortError'
  error.code = 'ABORT_ERR'
  return error
}

/**
 * Whether a configured command resolves without running it  - the whole of
 * `available()`. An absolute path is checked directly; a bare name is searched
 * on `PATH` (plus Windows executable extensions).
 *
 * @param {string} command - configured executable.
 * @returns {boolean} true when the executable looks present.
 */
export function commandResolves(command) {
  if (typeof command !== 'string' || command.trim() === '') return false
  if (isAbsolute(command)) return existsSync(command)
  const searchPath = process.env.PATH ?? process.env.Path ?? ''
  if (searchPath === '') return false
  const suffixes = process.platform === 'win32' && extname(command) === ''
    ? ['', '.exe', '.cmd', '.bat', '.com']
    : ['']
  for (const directory of searchPath.split(delimiter)) {
    if (!directory) continue
    for (const suffix of suffixes) {
      if (existsSync(join(directory, command + suffix))) return true
    }
  }
  return false
}

/**
 * Probe for `available()`: a positive answer is cached (the executable cannot
 * un-install underneath a running harness); a negative one is retried, so a
 * deployment can fix the command and have the provider become usable without a
 * reload.
 */
function createCommandProbe(getCommand) {
  let resolved = false
  return () => {
    if (resolved) return true
    resolved = commandResolves(getCommand())
    return resolved
  }
}

/**
 * Resolve a `ctx.logger`-shaped logger. cordis makes `ctx.logger` callable and
 * also gives it severity methods; both shapes (and a missing logger, as in a
 * test double) are accepted so the bridge can never fail because of logging.
 *
 * @param {{ logger?: unknown } | undefined} ctx - plugin context.
 * @returns {{ debug: Function, info: Function, warn: Function, error: Function }} named logger.
 */
function createLogger(ctx) {
  const noop = () => {}
  const fallback = { debug: noop, info: noop, warn: noop, error: noop }
  const raw = ctx?.logger
  if (!raw) return fallback
  if (typeof raw === 'function') {
    try {
      const named = raw('web-mcp')
      if (named && typeof named.info === 'function') return named
    } catch {}
  }
  if (typeof raw.info === 'function' || typeof raw.debug === 'function') {
    return {
      debug: typeof raw.debug === 'function' ? raw.debug.bind(raw) : noop,
      info: typeof raw.info === 'function' ? raw.info.bind(raw) : noop,
      warn: typeof raw.warn === 'function' ? raw.warn.bind(raw) : noop,
      error: typeof raw.error === 'function' ? raw.error.bind(raw) : noop,
    }
  }
  return fallback
}

// ---------------------------------------------------------------------------
// MCP stdio client
// ---------------------------------------------------------------------------

/**
 * A minimal MCP client for a stdio server: newline-delimited JSON-RPC 2.0 in
 * both directions, `initialize` handshake, `tools/call`, timeouts, cancellation
 * and automatic respawn after the child exits.
 *
 * Deliberately not built on `@modelcontextprotocol/sdk`: the DSH profile
 * resolves that SDK only through the harness's own dependency graph, and this
 * package has to load from a `link:` with no installed dependencies at all.
 */
export class McpStdioClient {
  #options
  #log
  #child = null
  #buffer = ''
  #nextId = 1
  #pending = new Map()
  #ready = null
  #closed = false
  #stderrTail = ''
  #exited = null
  #startedAt = 0
  #restarts = 0

  /**
   * @param {object} options - resolved plugin options plus `logger`.
   */
  constructor(options) {
    this.#options = options
    this.#log = options.logger
  }

  /** Whether a live, initialized child currently exists. */
  get connected() {
    return this.#child !== null && this.#ready !== null
  }

  /** Whether `close()` has been called (the client is then unusable). */
  get closed() {
    return this.#closed
  }

  /** How many times the child has been (re)spawned after an exit. */
  get restarts() {
    return this.#restarts
  }

  /** Process id of the live child (diagnostics only; `null` when not running). */
  get pid() {
    return this.#child?.pid ?? null
  }

  /**
   * Ensure a live, initialized child exists, spawning it on demand. Concurrent
   * callers share one handshake. A child that died is respawned by the next call.
   *
   * @returns {Promise<object>} the server's `initialize` result.
   */
  async ensure() {
    if (this.#closed) throw new Error('web-mcp: the MCP client is closed')
    if (this.#ready) return this.#ready
    this.#ready = this.#handshake().catch((error) => {
      this.#ready = null
      this.#reapChild()
      throw error
    })
    return this.#ready
  }

  /**
   * List the server's tools. Used by the warmup to prove the configured tool
   * names exist; failures are the caller's to interpret.
   *
   * @returns {Promise<Array<{ name?: string }>>} advertised tools.
   */
  async listTools() {
    await this.ensure()
    const result = await this.#request('tools/list', {}, this.#options.initializeTimeoutMs, undefined, 'tools/list')
    return Array.isArray(result?.tools) ? result.tools : []
  }

  /**
   * Call one MCP tool and return its raw `CallToolResult`.
   *
   * @param {string} toolName - raw MCP tool name.
   * @param {Record<string, unknown>} args - tool arguments.
   * @param {{ timeoutMs?: number, signal?: AbortSignal }} [options] - per-call options.
   * @returns {Promise<object>} the MCP result object.
   */
  async callTool(toolName, args, options = {}) {
    await this.ensure()
    const timeoutMs = options.timeoutMs ?? this.#options.toolCallTimeoutMs
    const result = await this.#request('tools/call', { name: toolName, arguments: args ?? {} }, timeoutMs, options.signal, `tools/call ${toolName}`)
    if (result && result.isError === true) throw new Error(describeToolFailure(toolName, result))
    return result ?? {}
  }

  /**
   * Close the child. Idempotent; disarms pending requests first so callers see a
   * closed-client error instead of waiting for their timeout.
   *
   * @returns {Promise<void>} resolves once the child is gone (or escalated).
   */
  async close() {
    this.#closed = true
    const child = this.#child
    for (const [id, entry] of [...this.#pending]) {
      this.#settle(id)
      entry.reject(new Error('web-mcp: the MCP client was closed'))
    }
    this.#child = null
    this.#ready = null
    if (!child) return
    try {
      child.stdin.end()
    } catch {}
    const exited = this.#exited ?? Promise.resolve()
    // A venv/launcher shim (`Scripts\python.exe` from uv, for example) runs the
    // real interpreter as its own child, and it does not always stop on stdin
    // EOF, so the tree is killed rather than just the direct child.
    if (await settlesWithin(exited, 250)) return
    killTree(child)
    if (await settlesWithin(exited, 500)) return
    try {
      child.kill('SIGKILL')
    } catch {}
    await settlesWithin(exited, 500)
  }

  /**
   * Best-effort synchronous teardown for a host that is exiting without
   * unwinding its disposers. Safe to call at any time; the client stays closed.
   */
  terminateNow() {
    this.#closed = true
    if (this.#child) killTree(this.#child)
  }

  /** Spawn the child and run the MCP handshake. */
  async #handshake() {
    const child = this.#spawn()
    const restarted = this.#restarts > 0
    const info = await this.#request(
      'initialize',
      {
        protocolVersion: this.#options.protocolVersion,
        capabilities: {},
        clientInfo: { name: this.#options.clientName, version: this.#options.clientVersion },
      },
      this.#options.initializeTimeoutMs,
      undefined,
      'initialize',
    )
    if (this.#child !== child) throw new Error('web-mcp: the MCP server exited during initialize')
    this.#notify('notifications/initialized')
    const server = info?.serverInfo && typeof info.serverInfo === 'object' ? info.serverInfo : {}
    const negotiated = typeof info?.protocolVersion === 'string' ? info.protocolVersion : 'unknown'
    this.#log.info(
      `LLMMcp MCP session ready (server=${String(server.name ?? 'unknown')} ${String(server.version ?? '')}, protocol=${negotiated}${restarted ? ', restarted' : ''})`,
    )
    return info ?? {}
  }

  /** Start the child process and wire its three pipes. */
  #spawn() {
    const { command, args, cwd, env } = this.#options
    if (!command) throw new Error('web-mcp: config.command is required (the MCP server executable)')
    const startedAt = Date.now()
    this.#log.debug(`spawning MCP server ${redactCredentials(command)} ${args.map(arg => redactCredentials(arg)).join(' ')}`.trim())
    const child = spawn(command, args, {
      cwd,
      env,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    })
    this.#child = child
    this.#buffer = ''
    this.#stderrTail = ''
    this.#startedAt = startedAt
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', chunk => this.#receive(chunk))
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', chunk => this.#receiveStderr(chunk))
    child.stdin.on('error', (error) => this.#log.debug(`MCP server stdin error: ${error.message}`))
    this.#exited = new Promise((resolve) => {
      child.once('close', (code, signal) => resolve({ code, signal }))
    })
    child.once('error', error => this.#onExit(child, { error }))
    child.once('close', (code, signal) => this.#onExit(child, { code, signal }))
    return child
  }

  /** Reject everything and forget the child (used when a handshake fails). */
  #reapChild() {
    const child = this.#child
    this.#child = null
    this.#ready = null
    if (!child) return
    try {
      child.kill()
    } catch {}
  }

  /** Handle the child going away exactly once. */
  #onExit(child, info) {
    if (this.#child !== child) return
    this.#child = null
    this.#ready = null
    if (this.#startedAt !== 0) this.#restarts += 1
    const reason = info.error
      ? `failed to start: ${info.error.message}`
      : `exited (code=${info.code ?? 'null'}, signal=${info.signal ?? 'null'})`
    const error = new Error(`web-mcp: the LLMMcp MCP server ${reason}${this.#stderrSuffix()}`)
    for (const [id, entry] of [...this.#pending]) {
      this.#settle(id)
      entry.reject(error)
    }
    if (this.#closed) return
    this.#log.warn(`${error.message}; the next web_search/web_fetch call respawns it`)
  }

  /** Consume stdout, dispatching every complete newline-delimited frame. */
  #receive(chunk) {
    this.#buffer += chunk
    let index = this.#buffer.indexOf('\n')
    while (index >= 0) {
      const line = this.#buffer.slice(0, index).replace(/\r$/, '')
      this.#buffer = this.#buffer.slice(index + 1)
      if (line.trim() !== '') this.#dispatch(line)
      index = this.#buffer.indexOf('\n')
    }
    if (this.#buffer.length > MAX_FRAME_CHARS) {
      this.#log.warn('dropping an oversized MCP frame on stdout')
      this.#buffer = ''
    }
  }

  /** Parse one frame and route it. */
  #dispatch(line) {
    let message
    try {
      message = JSON.parse(line)
    } catch {
      // Never surface the raw line: it may be a stray print from the server and
      // this channel must stay clean for diagnostics only.
      this.#log.debug('ignored a non-JSON line on the MCP stdout channel')
      return
    }
    if (!message || typeof message !== 'object') return
    if (typeof message.method === 'string') {
      if (message.id !== undefined && message.id !== null) this.#answerServerRequest(message)
      else this.#log.debug(`notification from LLMMcp: ${message.method}`)
      return
    }
    if (message.id === undefined || message.id === null) return
    const entry = this.#pending.get(message.id)
    if (!entry) {
      this.#log.debug(`ignored a response for unknown request id ${String(message.id)}`)
      return
    }
    this.#settle(message.id)
    if (message.error) entry.reject(toMcpError(entry.label, message.error))
    else entry.resolve(message.result ?? {})
  }

  /** Answer a server鈫抍lient request; only `ping` is supported. */
  #answerServerRequest(message) {
    if (message.method === 'ping') {
      this.#write({ jsonrpc: '2.0', id: message.id, result: {} })
      return
    }
    this.#log.debug(`refusing unsupported server request ${message.method}`)
    this.#write({
      jsonrpc: '2.0',
      id: message.id,
      error: { code: -32601, message: `method ${message.method} is not supported by dsh-web-mcp` },
    })
  }

  /** Keep the tail of the child's stderr for error messages, redacted. */
  #receiveStderr(chunk) {
    const text = redactCredentials(chunk)
    this.#stderrTail = (this.#stderrTail + text).slice(-this.#options.stderrTailChars)
    for (const line of text.split(/\r?\n/)) {
      if (line.trim() !== '') this.#log.debug(`LLMMcp stderr: ${line}`)
    }
  }

  /** The stderr tail as an error-message suffix. */
  #stderrSuffix() {
    const tail = this.#stderrTail.trim()
    return tail === '' ? '' : `\n--- LLMMcp stderr (tail) ---\n${bound(tail, this.#options.stderrTailChars).trim()}`
  }

  /** Write one JSON-RPC object to the child's stdin. */
  #write(payload) {
    const child = this.#child
    if (!child || !child.stdin || child.stdin.destroyed) return false
    try {
      child.stdin.write(`${JSON.stringify(payload)}\n`)
      return true
    } catch (error) {
      this.#log.debug(`failed to write to the MCP server: ${error.message}`)
      return false
    }
  }

  /** Send a JSON-RPC notification (no id, no response expected). */
  #notify(method, params) {
    const message = { jsonrpc: '2.0', method }
    if (params !== undefined) message.params = params
    if (!this.#write(message)) this.#log.debug(`could not send notification ${method}`)
  }

  /**
   * Issue one request and await its matching response.
   *
   * @param {string} method - JSON-RPC method.
   * @param {object|undefined} params - request params.
   * @param {number} timeoutMs - per-request budget.
   * @param {AbortSignal|undefined} signal - cancellation.
   * @param {string} label - name used in diagnostics.
   * @returns {Promise<any>} the `result` member.
   */
  #request(method, params, timeoutMs, signal, label = method) {
    const child = this.#child
    if (!child) return Promise.reject(new Error('web-mcp: the MCP server process is not running'))
    if (signal?.aborted) return Promise.reject(abortError())
    const id = this.#nextId++
    const message = { jsonrpc: '2.0', id, method }
    if (params !== undefined) message.params = params
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#settle(id)
        reject(new Error(`web-mcp: MCP ${label} timed out after ${timeoutMs} ms${this.#stderrSuffix()}`))
      }, timeoutMs)
      if (typeof timer.unref === 'function') timer.unref()
      const entry = { label, resolve, reject, timer, signal, onAbort: null }
      if (signal) {
        entry.onAbort = () => {
          if (!this.#pending.has(id)) return
          this.#settle(id)
          this.#notify('notifications/cancelled', { requestId: id, reason: 'aborted' })
          reject(abortError())
        }
        signal.addEventListener('abort', entry.onAbort, { once: true })
      }
      this.#pending.set(id, entry)
      if (!this.#write(message)) {
        this.#settle(id)
        reject(new Error(`web-mcp: could not write to the MCP server (${label})${this.#stderrSuffix()}`))
      }
    })
  }

  /** Clear a pending entry's timer/listener and drop it. */
  #settle(id) {
    const entry = this.#pending.get(id)
    if (!entry) return
    clearTimeout(entry.timer)
    if (entry.signal && entry.onAbort) entry.signal.removeEventListener('abort', entry.onAbort)
    this.#pending.delete(id)
  }
}

/** Turn a JSON-RPC error object into an Error carrying code, message and data. */
function toMcpError(label, error) {
  const code = error && typeof error.code === 'number' ? error.code : undefined
  const message = error && typeof error.message === 'string' ? error.message : JSON.stringify(error)
  const data = error && error.data !== undefined ? ` (data: ${bound(JSON.stringify(error.data), 800)})` : ''
  const wrapped = new Error(`web-mcp: MCP ${label} failed${code === undefined ? '' : ` [${code}]`}: ${message}${data}`)
  if (code !== undefined) wrapped.code = code
  return wrapped
}

/** Concatenate the text blocks of an MCP result. */
function textBlocks(result) {
  if (!result || !Array.isArray(result.content)) return []
  const out = []
  for (const block of result.content) {
    if (block && block.type === 'text' && typeof block.text === 'string') out.push(block.text)
  }
  return out
}

/** Build the error text for an MCP `isError` tool result (LLMMcp sends JSON). */
function describeToolFailure(toolName, result) {
  const text = textBlocks(result).join('\n').trim()
  let message = text
  let code
  let hint
  if (text.startsWith('{')) {
    try {
      const parsed = JSON.parse(text)
      const error = parsed && typeof parsed === 'object' ? parsed.error ?? parsed : undefined
      if (error && typeof error.message === 'string') message = error.message
      if (error && typeof error.code === 'string') code = error.code
      if (error && typeof error.hint === 'string') hint = error.hint
    } catch {}
  }
  const prefix = `web-mcp: LLMMcp tool "${toolName}" failed${code ? ` [${code}]` : ''}`
  const detail = message ? `: ${bound(message, 2000)}` : ''
  const suffix = hint ? ` (hint: ${bound(hint, 800)})` : ''
  return `${prefix}${detail}${suffix}`
}

/**
 * Read the structured payload of an MCP tool result: `structuredContent` when
 * the server sent one, else a JSON text block (a server without output schemas).
 *
 * @param {object} result - MCP CallToolResult.
 * @param {string} toolName - tool name, for the error message.
 * @returns {Record<string, any>} the payload object.
 */
function toolPayload(result, toolName) {
  const structured = result?.structuredContent
  if (structured && typeof structured === 'object' && !Array.isArray(structured)) return structured
  const text = textBlocks(result).join('\n').trim()
  if (text.startsWith('{')) {
    try {
      const parsed = JSON.parse(text)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed
    } catch {}
  }
  const hint = text === '' ? 'the response carried no text block' : `text was: ${bound(text, 400)}`
  throw new Error(`web-mcp: LLMMcp tool "${toolName}" returned no structured payload (${hint})`)
}

/** Wrap a transport/tool failure with the calling capability's context. */
function wrapToolError(operation, toolName, error) {
  if (error && (error.name === 'AbortError' || error.code === 'ABORT_ERR')) return error
  const message = error instanceof Error ? error.message : String(error)
  const wrapped = new Error(`web-mcp: LLMMcp ${operation} via "${toolName}" failed: ${message}`, { cause: error })
  return wrapped
}

// ---------------------------------------------------------------------------
// providers
// ---------------------------------------------------------------------------

/**
 * Search provider backed by LLMMcp's `web_search`.
 *
 * `available()` is a pure local check (configured command resolves) so provider
 * selection never performs I/O; the child process is started lazily by the
 * first call.
 */
export class LLMMcpSearchProvider {
  #client
  #tool
  #timeoutMs
  #requestTimeoutS
  #probe
  #log

  /**
   * @param {object} options - `id`, `client`, `searchTool`, `toolCallTimeoutMs`,
   *   `commandAvailable` and `logger`.
   */
  constructor(options) {
    this.id = options.id
    this.#client = options.client
    this.#tool = options.searchTool
    this.#timeoutMs = options.toolCallTimeoutMs
    this.#requestTimeoutS = options.searchTimeoutS
    this.#probe = options.commandAvailable
    this.#log = options.logger
  }

  /** Cheap local usability check; never touches the network. */
  available() {
    return this.#probe()
  }

  /**
   * Run one search through LLMMcp and map it onto the seam's vocabulary.
   *
   * @param {{ query: string, maxResults?: number }} request - the seam request.
   * @param {AbortSignal} [signal] - cancellation.
   * @returns {Promise<{ sources: Array<{ url: string, title?: string, snippet?: string, publishedAt?: string }>, truncated: boolean, content?: string }>} the normalised result.
   */
  async search(request, signal) {
    const query = typeof request?.query === 'string' ? request.query.trim() : ''
    if (query === '') throw new Error('web-mcp: search requires a non-empty query')
    const maxResults = Number.isInteger(request?.maxResults) && request.maxResults > 0 ? request.maxResults : undefined
    const args = { query, timeout: this.#requestTimeoutS }
    if (maxResults !== undefined) args.max_results = maxResults
    const startedAt = Date.now()
    let result
    try {
      result = await this.#client.callTool(this.#tool, args, { timeoutMs: this.#timeoutMs, signal })
    } catch (error) {
      throw wrapToolError('search', this.#tool, error)
    }
    const payload = toolPayload(result, this.#tool)
    const raw = Array.isArray(payload.results) ? payload.results : []
    const sources = []
    for (const item of raw) {
      const url = item && typeof item.url === 'string' ? item.url : ''
      if (url === '') continue
      const source = { url }
      if (typeof item.title === 'string' && item.title !== '') source.title = item.title
      if (typeof item.snippet === 'string' && item.snippet !== '') source.snippet = item.snippet
      if (typeof item.published_at === 'string' && item.published_at !== '') source.publishedAt = item.published_at
      sources.push(source)
    }
    const truncated = maxResults !== undefined && sources.length > maxResults
    const limited = truncated ? sources.slice(0, maxResults) : sources
    const out = { sources: limited, truncated }
    if (typeof payload.answer === 'string' && payload.answer.trim() !== '') out.content = payload.answer
    this.#log.debug(
      `web_search "${query}" 鈫?${limited.length} source(s) in ${Date.now() - startedAt} ms (engine=${String(payload.engine ?? 'unknown')})`,
    )
    return out
  }
}

/**
 * Fetch provider backed by LLMMcp's `web_fetch` in `html` form: `dsh-tool-web`
 * converts HTML to markdown itself and owns the presentation budget, so the
 * bridge keeps the provider request/result contract exactly as declared.
 */
export class LLMMcpFetchProvider {
  #client
  #tool
  #timeoutMs
  #format
  #maxChars
  #requestTimeoutS
  #probe
  #log

  /**
   * @param {object} options - `id`, `client`, `fetchTool`, `toolCallTimeoutMs`,
   *   `fetchFormat`, `fetchMaxChars`, `commandAvailable` and `logger`.
   */
  constructor(options) {
    this.id = options.id
    this.#client = options.client
    this.#tool = options.fetchTool
    this.#timeoutMs = options.toolCallTimeoutMs
    this.#requestTimeoutS = options.fetchTimeoutS
    this.#format = options.fetchFormat
    this.#maxChars = options.fetchMaxChars
    this.#probe = options.commandAvailable
    this.#log = options.logger
  }

  /** Cheap local usability check; never touches the network. */
  available() {
    return this.#probe()
  }

  /**
   * Retrieve one URL through LLMMcp and map it onto the seam's vocabulary.
   *
   * @param {{ url: string }} request - the seam request.
   * @param {AbortSignal} [signal] - cancellation.
   * @returns {Promise<{ url: string, statusCode: number, body: { kind: 'html'|'text', content: string }, truncated: boolean }>} the normalised result.
   */
  async fetch(request, signal) {
    const url = typeof request?.url === 'string' ? request.url.trim() : ''
    if (url === '') throw new Error('web-mcp: fetch requires a non-empty url')
    const startedAt = Date.now()
    let result
    try {
      result = await this.#client.callTool(
        this.#tool,
        { url, format: this.#format, max_chars: this.#maxChars, timeout: this.#requestTimeoutS },
        { timeoutMs: this.#timeoutMs, signal },
      )
    } catch (error) {
      throw wrapToolError('fetch', this.#tool, error)
    }
    const payload = toolPayload(result, this.#tool)
    const content = typeof payload.content === 'string' ? payload.content : ''
    const contentType = typeof payload.content_type === 'string' ? payload.content_type : ''
    const statusCode = Number.isInteger(payload.status)
      ? payload.status
      : Number.isInteger(payload.statusCode) ? payload.statusCode : 0
    const finalUrl = nonEmptyString(payload.final_url) ?? nonEmptyString(payload.url) ?? url
    const out = {
      url: finalUrl,
      statusCode,
      body: { kind: bodyKind(contentType, content, this.#format), content },
      truncated: payload.truncated === true,
    }
    this.#log.debug(
      `web_fetch ${url} 鈫?HTTP ${statusCode}, ${content.length} chars in ${Date.now() - startedAt} ms`,
    )
    return out
  }
}

/** First non-empty string of a candidate value, else undefined. */
function nonEmptyString(value) {
  return typeof value === 'string' && value.trim() !== '' ? value : undefined
}

/**
 * Classify a fetched body for the seam: `html` when the payload is markup (the
 * provider's own content type, or the document itself when the type is missing).
 */
function bodyKind(contentType, content, requestedFormat = 'html') {
  if (requestedFormat !== 'html') return 'text'
  const type = contentType.toLowerCase()
  if (type.includes('html')) return 'html'
  if (type.startsWith('text/') || type.includes('json') || type.includes('xml')) return 'text'
  const head = content.slice(0, 512).trimStart().toLowerCase()
  return head.startsWith('<!doctype html') || head.startsWith('<html') ? 'html' : 'text'
}

// ---------------------------------------------------------------------------
// plugin
// ---------------------------------------------------------------------------

/**
 * Register the two providers on `ctx.web` and own the MCP child's lifetime.
 *
 * The child is spawned eagerly when `warmup` is on (so the first tool call does
 * not pay the interpreter start), and disposed with the plugin's fiber.
 *
 * @param {object} ctx - cordis context (must carry `web`).
 * @param {object} [config] - loader entry config; see {@link DEFAULTS}.
 * @returns {void}
 */
export function apply(ctx, config) {
  const options = resolveOptions(config)
  const logger = createLogger(ctx)
  const commandAvailable = createCommandProbe(() => options.command)
  const env = buildChildEnv(options.env)
  // The client keeps its own options object, so a runtime resolved after
  // registration has to be written into that object as well.
  const childOptions = { ...options, env, logger }
  const client = new McpStdioClient(childOptions)
  const shared = {
    id: options.providerId,
    client,
    toolCallTimeoutMs: options.toolCallTimeoutMs,
    searchTimeoutS: options.searchTimeoutS,
    fetchTimeoutS: options.fetchTimeoutS,
    commandAvailable,
    logger,
  }
  const searchProvider = new LLMMcpSearchProvider({ ...shared, searchTool: options.searchTool })
  const fetchProvider = new LLMMcpFetchProvider({
    ...shared,
    fetchTool: options.fetchTool,
    fetchFormat: options.fetchFormat,
    fetchMaxChars: options.fetchMaxChars,
  })

  ctx.web.registerSearchProvider(searchProvider)
  ctx.web.registerFetchProvider(fetchProvider)
  logger.info(
    `ctx.web now has providers "${options.providerId}" (search=${options.searchTool}, fetch=${options.fetchTool}) via ${options.command || '<unset command>'}`,
  )

  // A fresh machine has the plugin but no interpreter with LLMMcp installed.
  // Resolve (and, when allowed, install) it in the background so provider
  // registration stays synchronous and a failure never blocks the harness.
  if (!commandResolves(options.command)) {
    void resolveRuntime(options, logger, commandResolves).then(runtime => {
      if (runtime === null) {
        logger.warn(
          `LLMMcp is not available yet; install it or set config.command to an interpreter with llmmcp (state: ${options.autoInstall ? 'auto-install failed' : 'auto-install disabled'})`,
        )
        return
      }
      options.command = runtime.command
      childOptions.command = runtime.command
      logger.info(`LLMMcp runtime resolved from ${runtime.source}: ${runtime.command}`)
      if (options.warmup) {
        void client.ensure().catch(error => {
          logger.debug(`LLMMcp warmup after bootstrap failed: ${error instanceof Error ? error.message : String(error)}`)
        })
      }
    })
  }

  if (typeof ctx.effect === 'function') {
    ctx.effect(() => {
      // A hard host quit does not unwind effects, and only a synchronous kill
      // then keeps the MCP child from outliving the harness.
      const onExit = () => client.terminateNow()
      process.once('exit', onExit)
      return () => {
        process.removeListener('exit', onExit)
        return client.close()
      }
    }, 'web-mcp.mcp-client')
  }

  if (options.warmup) {
    void client
      .ensure()
      .then(async () => {
        const tools = await client.listTools().catch(() => [])
        const names = tools.map(tool => String(tool?.name ?? '')).filter(Boolean)
        const missing = [options.searchTool, options.fetchTool].filter(tool => !names.includes(tool))
        if (names.length > 0 && missing.length > 0) {
          logger.warn(`LLMMcp does not advertise ${missing.join(', ')}; advertised: ${names.join(', ')}`)
        } else {
          logger.debug(`LLMMcp tools available: ${names.join(', ')}`)
        }
      })
      .catch((error) => {
        logger.warn(`LLMMcp warmup failed (calls will retry and report the error): ${error.message}`)
      })
  }
}

export default { name, inject, apply }
