/**
 * Exercise the bootstrap resolver without touching the real deployment.
 *
 * Run with: node scripts/verify-runtime.mjs [--install]
 *   --install also performs a real venv + pip install into a temp directory.
 */

import { existsSync, mkdtempSync, rmSync, writeFileSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  bootstrapRuntime,
  llmmcpHome,
  readLocalConfig,
  resolveRuntime,
  venvPython,
  writeLocalConfig,
} from '../runtime.mjs'

const logger = {
  debug: () => {},
  info: message => console.log(`  info: ${message}`),
  warn: message => console.log(`  warn: ${message}`),
  error: message => console.log(`  error: ${message}`),
}

function assert(condition, message) {
  if (!condition) throw new Error(`assertion failed: ${message}`)
  console.log(`  ok: ${message}`)
}

const workspace = mkdtempSync(join(tmpdir(), 'llmmcp-runtime-'))
const exists = path => typeof path === 'string' && path !== '' && existsSync(path)

try {
  console.log('1) configured command wins')
  const configured = await resolveRuntime(
    { command: 'C:/Windows/System32/cmd.exe', llmmcpHome: join(workspace, 'home-a'), autoInstall: false },
    logger,
    exists,
  )
  assert(configured?.source === 'config', 'configured command is used')

  console.log('2) machine-local config is reused')
  const home = join(workspace, 'home-b')
  mkdirSync(home, { recursive: true })
  writeFileSync(join(home, 'config.json'), JSON.stringify({ command: 'C:/Windows/System32/cmd.exe' }), 'utf8')
  const recorded = await resolveRuntime({ llmmcpHome: home, autoInstall: false }, logger, exists)
  assert(recorded?.source === 'local-config', 'recorded command is reused')

  console.log('3) autoInstall off reports unavailable instead of installing')
  const unavailable = await resolveRuntime(
    { llmmcpHome: join(workspace, 'home-c'), autoInstall: false },
    logger,
    () => false,
  )
  assert(unavailable === null, 'no install when autoInstall is false')

  if (process.argv.includes('--install')) {
    console.log('4) real bootstrap into a temp venv')
    const installerHome = join(workspace, 'home-d')
    const venvDir = join(workspace, 'venv')
    const result = await bootstrapRuntime(
      {
        llmmcpHome: installerHome,
        venvDir,
        autoInstall: true,
        installSpec:
          'https://github.com/ai-v5/LLMMcp/releases/download/v0.1.2/llmmcp-0.1.2-py3-none-any.whl',
      },
      logger,
    )
    assert(existsSync(venvPython(venvDir)), 'venv interpreter exists')
    assert(result.command === venvPython(venvDir), 'bootstrap returns the venv interpreter')
    const saved = readLocalConfig({ llmmcpHome: installerHome })
    assert(saved.command === result.command, 'command recorded in config.json')
    assert(llmmcpHome({}) .endsWith('.llmmcp'), 'default home is ~/.llmmcp')
  }

  console.log('runtime verification complete')
} finally {
  rmSync(workspace, { recursive: true, force: true })
}
