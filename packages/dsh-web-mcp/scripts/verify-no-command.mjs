/**
 * Check the plugin with no configured command: providers must register, stay
 * unavailable, and never attempt an install when autoInstall is off.
 *
 * Run with: node scripts/verify-no-command.mjs
 */

import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const plugin = await import('../index.js')

const providers = { search: [], fetch: [] }
const messages = []
const logger = {
  debug: message => messages.push(`debug: ${message}`),
  info: message => messages.push(`info: ${message}`),
  warn: message => messages.push(`warn: ${message}`),
  error: message => messages.push(`error: ${message}`),
}
const ctx = {
  web: {
    registerSearchProvider(provider) {
      providers.search.push(provider)
      return () => {}
    },
    registerFetchProvider(provider) {
      providers.fetch.push(provider)
      return () => {}
    },
  },
  logger,
  effect: () => {},
  get: () => undefined,
}

const workspace = mkdtempSync(join(tmpdir(), 'llmmcp-nocmd-'))
try {
  plugin.apply(ctx, { autoInstall: false, llmmcpHome: join(workspace, 'home'), warmup: false, command: '' })
  console.log('registered:', `search=${providers.search.length}`, `fetch=${providers.fetch.length}`)
  console.log('available:', providers.search[0]?.available(), providers.fetch[0]?.available())
  const installAttempted = messages.some(line => line.includes('creating venv') || line.includes('installing'))
  console.log('install attempted:', installAttempted)
  if (providers.search.length !== 1 || providers.fetch.length !== 1) throw new Error('providers not registered')
  if (providers.search[0].available()) throw new Error('provider must be unavailable without a command')
  if (installAttempted) throw new Error('autoInstall=false must not install')
  console.log('no-command behaviour OK')
} finally {
  rmSync(workspace, { recursive: true, force: true })
}
