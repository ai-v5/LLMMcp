/**
 * Runtime bootstrap: install the LLMMcp Python server on demand.
 *
 * The bridge is a shell with no runtime of its own, so a fresh machine has the
 * plugin but no `llmmcp`. This module closes that gap: it reuses a configured
 * command when one resolves, then a previously bootstrapped interpreter, and only
 * then creates `~/.llmmcp/venv` and installs the server from a published wheel.
 *
 * Every step is a fixed argument list with `shell: false` and `windowsHide: true`,
 * so no console window appears and nothing is interpolated into a shell line. The
 * resolved interpreter is remembered in `~/.llmmcp/config.json`, outside the
 * repository, so the next start needs no network.
 *
 * Nothing here is fatal: a failure returns a structured error and the plugin stays
 * loaded with unusable providers, exactly like an unreachable MCP server.
 *
 * @module llm-dsh-web-mcp/runtime
 */

import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, isAbsolute, join } from 'node:path'

/** Published wheel; override with `installSpec` when testing a local build. */
export const DEFAULT_INSTALL_SPEC =
  'https://github.com/ai-v5/LLMMcp/releases/download/v0.1.1/llmmcp-0.1.1-py3-none-any.whl'

/** Interpreters probed when neither config nor the local config file names one. */
const PYTHON_CANDIDATES = process.platform === 'win32'
  ? ['py', 'python', 'python3']
  : ['python3', 'python']

const MIN_PYTHON = [3, 10]

/**
 * Directory holding the machine-local configuration and the managed venv.
 *
 * @param {{ llmmcpHome?: string }} options - resolved plugin options.
 * @returns {string} absolute path.
 */
export function llmmcpHome(options) {
  const configured = options?.llmmcpHome
  if (typeof configured === 'string' && configured.trim() !== '') return configured
  return join(homedir(), '.llmmcp')
}

/**
 * Read the machine-local configuration written by an earlier bootstrap.
 *
 * @param {object} options - resolved plugin options.
 * @returns {Record<string, unknown>} the parsed object, or an empty one.
 */
export function readLocalConfig(options) {
  const file = join(llmmcpHome(options), 'config.json')
  try {
    const parsed = JSON.parse(readFileSync(file, 'utf8'))
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

/**
 * Merge `patch` into the machine-local configuration.
 *
 * @param {object} options - resolved plugin options.
 * @param {Record<string, unknown>} patch - keys to write.
 * @returns {string} the file path that was written.
 */
export function writeLocalConfig(options, patch) {
  const home = llmmcpHome(options)
  mkdirSync(home, { recursive: true })
  const file = join(home, 'config.json')
  const next = { ...readLocalConfig(options), ...patch }
  writeFileSync(file, JSON.stringify(next, null, 2), 'utf8')
  return file
}

/**
 * Pick the interpreter for a new venv and confirm it is new enough.
 *
 * @param {object} options - resolved plugin options.
 * @returns {Promise<{ command: string, args: string[], version: string }>}
 * @throws when no candidate runs or every candidate is too old.
 */
export async function findPython(options) {
  const configured = typeof options?.python === 'string' ? options.python.trim() : ''
  const candidates = configured !== '' ? [configured] : PYTHON_CANDIDATES
  const failures = []
  for (const candidate of candidates) {
    const probe = await runCapture(candidate, ['-c', 'import sys;print("%d.%d" % sys.version_info[:2])'], 20000)
    if (!probe.ok) {
      failures.push(`${candidate}: ${probe.reason}`)
      continue
    }
    const version = probe.stdout.trim()
    const [major, minor] = version.split('.').map(Number)
    if (major !== MIN_PYTHON[0] || minor < MIN_PYTHON[1]) {
      failures.push(`${candidate}: python ${version} is older than ${MIN_PYTHON.join('.')}`)
      continue
    }
    return { command: candidate, args: [], version }
  }
  throw new RuntimeError(
    'no usable Python interpreter found',
    'PYTHON_NOT_FOUND',
    `install Python ${MIN_PYTHON.join('.')}+ or set config.python to its executable (tried: ${failures.join('; ')})`,
  )
}

/**
 * Path of the interpreter a venv owns.
 *
 * @param {string} venvDir - venv directory.
 * @returns {string} the interpreter inside it.
 */
export function venvPython(venvDir) {
  return process.platform === 'win32'
    ? join(venvDir, 'Scripts', 'python.exe')
    : join(venvDir, 'bin', 'python')
}

/**
 * Resolve the command to start the MCP server, or explain why none is available.
 *
 * Order: the configured `command`, then the interpreter an earlier bootstrap
 * recorded, then a fresh install when `autoInstall` is on.
 *
 * @param {object} options - resolved plugin options.
 * @param {{ debug: Function, info: Function, warn: Function, error: Function }} logger - plugin logger.
 * @param {(command: string) => boolean} resolvesCommand - executable probe.
 * @returns {Promise<{ command: string, source: string } | null>} the command, or null when unavailable.
 */
export async function resolveRuntime(options, logger, resolvesCommand) {
  if (options?.command && resolvesCommand(options.command)) {
    return { command: options.command, source: 'config' }
  }
  const local = readLocalConfig(options)
  const recorded = typeof local.command === 'string' ? local.command : ''
  if (recorded && resolvesCommand(recorded)) {
    logger.info(`LLMMcp runtime reused from ${join(llmmcpHome(options), 'config.json')}`)
    return { command: recorded, source: 'local-config' }
  }
  if (options?.autoInstall === false) {
    logger.warn('LLMMcp runtime missing and autoInstall is off; set config.command to an interpreter with llmmcp installed')
    return null
  }
  try {
    return await bootstrapRuntime(options, logger)
  } catch (error) {
    logger.error(`LLMMcp runtime bootstrap failed: ${error instanceof Error ? error.message : String(error)}`)
    if (error instanceof RuntimeError && error.hint) logger.error(`  hint: ${error.hint}`)
    return null
  }
}

/**
 * Create `~/.llmmcp/venv` (when needed), install the server and remember it.
 *
 * @param {object} options - resolved plugin options.
 * @param {{ debug: Function, info: Function, warn: Function, error: Function }} logger - plugin logger.
 * @returns {Promise<{ command: string, source: string }>}
 */
export async function bootstrapRuntime(options, logger) {
  const venvDir = typeof options.venvDir === 'string' && options.venvDir.trim() !== ''
    ? options.venvDir
    : join(llmmcpHome(options), 'venv')
  const spec = typeof options.installSpec === 'string' && options.installSpec.trim() !== ''
    ? options.installSpec
    : DEFAULT_INSTALL_SPEC
  const timeoutMs = Number.isInteger(options.installTimeoutMs) && options.installTimeoutMs > 0
    ? options.installTimeoutMs
    : 900000
  const environment = { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', PIP_DISABLE_PIP_VERSION_CHECK: '1' }

  const target = venvPython(venvDir)
  if (!existsSync(target)) {
    const python = await findPython(options)
    logger.info(`LLMMcp: creating venv with ${python.command} (python ${python.version}) at ${venvDir}`)
    const created = await runCapture(python.command, ['-m', 'venv', venvDir], timeoutMs, environment)
    if (!created.ok) {
      throw new RuntimeError(
        `could not create a venv at ${venvDir}: ${created.reason}`,
        'VENV_FAILED',
        'check that the interpreter can create venvs (the python3-venv package on Debian)',
      )
    }
  }

  const installed = await runCapture(target, ['-c', 'import llmmcp'], 30000, environment)
  if (!installed.ok) {
    logger.info(`LLMMcp: installing ${spec} into ${venvDir} (this can take a minute)`)
    const pipArgs = ['-m', 'pip', 'install', '--no-input', spec]
    if (spec.startsWith('http')) pipArgs.splice(3, 0, '--disable-pip-version-check')
    const piped = await runCapture(target, pipArgs, timeoutMs, environment)
    if (!piped.ok) {
      throw new RuntimeError(
        `pip install failed: ${piped.reason}`,
        'INSTALL_FAILED',
        'behind a proxy, export HTTPS_PROXY so pip can reach the index or the release URL',
      )
    }
    const verified = await runCapture(target, ['-c', 'import llmmcp;print(llmmcp.__version__)'], 60000, environment)
    if (!verified.ok) {
      throw new RuntimeError(`the installed server does not import: ${verified.reason}`, 'INSTALL_BROKEN', 'delete the venv and retry')
    }
    logger.info(`LLMMcp: installed llmmcp ${verified.stdout.trim()}`)
  }

  const file = writeLocalConfig(options, {
    command: target,
    install_spec: spec,
    installed_at: new Date().toISOString(),
  })
  logger.info(`LLMMcp: runtime ready at ${target} (recorded in ${file})`)
  return { command: target, source: 'bootstrap' }
}

/**
 * Run one fixed command and capture its outcome instead of throwing.
 *
 * @param {string} command - executable.
 * @param {string[]} args - argument list.
 * @param {number} timeoutMs - hard budget.
 * @param {NodeJS.ProcessEnv} [env] - child environment.
 * @returns {Promise<{ ok: boolean, code: number | null, stdout: string, stderr: string, reason: string }>}
 */
export function runCapture(command, args, timeoutMs, env) {
  return new Promise(resolve => {
    let stdout = ''
    let stderr = ''
    let settled = false
    let child
    try {
      child = spawn(command, args, {
        env: env ?? process.env,
        stdio: ['ignore', 'pipe', 'pipe'],
        shell: false,
        windowsHide: true,
      })
    } catch (error) {
      resolve({ ok: false, code: null, stdout: '', stderr: '', reason: error instanceof Error ? error.message : String(error) })
      return
    }
    const finish = result => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(result)
    }
    const timer = setTimeout(() => {
      try {
        child.kill()
      } catch {
        // The process is already gone; the timeout result still stands.
      }
      finish({ ok: false, code: null, stdout, stderr, reason: `timed out after ${timeoutMs} ms` })
    }, timeoutMs)
    child.stdout?.on('data', chunk => {
      stdout += String(chunk)
    })
    child.stderr?.on('data', chunk => {
      stderr += String(chunk)
    })
    child.on('error', error => {
      finish({ ok: false, code: null, stdout, stderr, reason: error instanceof Error ? error.message : String(error) })
    })
    child.on('close', code => {
      const ok = code === 0
      const tail = stderr.trim().split('\n').slice(-3).join(' | ')
      finish({ ok, code, stdout, stderr, reason: ok ? '' : `exit ${code}${tail ? `: ${tail}` : ''}` })
    })
  })
}

/** A bootstrap failure with a stable code and an operator-facing hint. */
export class RuntimeError extends Error {
  /**
   * @param {string} message - what failed.
   * @param {string} code - stable identifier for logs and tests.
   * @param {string} [hint] - what the operator can do.
   */
  constructor(message, code, hint) {
    super(message)
    this.name = 'RuntimeError'
    this.code = code
    this.hint = hint
  }
}

/**
 * Whether a path looks absolute, so callers do not need node:path in tests.
 *
 * @param {string} value - candidate path.
 * @returns {boolean} true for absolute paths.
 */
export function looksAbsolute(value) {
  return typeof value === 'string' && isAbsolute(value)
}

/**
 * Parent directory of a path, exposed for callers that only need the venv root.
 *
 * @param {string} value - file path.
 * @returns {string} its directory.
 */
export function parentOf(value) {
  return dirname(value)
}
