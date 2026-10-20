#!/usr/bin/env node
/**
 * Offline check of the profile wiring — the half `verify.mjs` cannot see.
 *
 * It rebuilds the effective entry list the way the DSH launcher does: every
 * bundle patch in `dsh.profile.bundles` order, then the profile's own
 * `cordis.patch.yml`, folded by `applyEntryPatches` (the same function the boot
 * include runs). Then it asserts
 *
 *   1. an entry `web-mcp` exists with a usable `command` / `args` / `cwd`,
 *   2. the `web` entry selects that `providerId` for search *and* fetch,
 *   3. no patch was skipped — a skipped patch is how a config typo silently
 *      does nothing,
 *   4. the plugin resolves from the profile directory by bare package name.
 *
 * Read-only. When the packaged desktop installation's bundles are not
 * resolvable from disk (they live inside `app.asar`), the check composes the
 * layers it *can* resolve and says so: the `web` row lives in
 * `@deepseek-ai/dsh-base`, which is always among them.
 *
 *   node scripts/check-profile.mjs [--profile desktop] [--home <DSH_HOME>]
 */

import { createRequire } from 'node:module'
import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { pathToFileURL } from 'node:url'
import process from 'node:process'

function parseArgs(argv) {
  const options = { profile: 'desktop', home: process.env.DSH_HOME ?? join(homedir(), '.dsh') }
  for (let index = 0; index < argv.length; index += 1) {
    switch (argv[index]) {
      case '--profile': options.profile = argv[++index]; break
      case '--home': options.home = argv[++index]; break
      case '--help': case '-h':
        process.stdout.write('usage: node scripts/check-profile.mjs [--profile <name>] [--home <DSH_HOME>]\n')
        process.exit(0)
        break
      default: throw new Error(`check-profile: unknown option ${argv[index]}`)
    }
  }
  return options
}

/** Import a module that only the DSH profile can resolve. */
async function importFrom(anchor, packageName) {
  const entry = createRequire(anchor).resolve(packageName)
  return import(pathToFileURL(entry).href)
}

/** Probe Node's own resolution paths for a package directory (as app-boot does). */
function packageDirFrom(anchor, packageName) {
  for (const searchPath of createRequire(anchor).resolve.paths(packageName) ?? []) {
    const candidate = join(searchPath, packageName)
    if (existsSync(join(candidate, 'package.json'))) return candidate
  }
  return undefined
}

/** Depth-first search for an entry id, entering groups. */
function findEntry(entries, id) {
  for (const entry of entries) {
    if (!entry || typeof entry !== 'object') continue
    if (entry.id === id) return entry
    if (entry.group && Array.isArray(entry.config)) {
      const nested = findEntry(entry.config, id)
      if (nested) return nested
    }
  }
  return undefined
}

/** Format applyEntryPatches' printf-style warning (`%C` = JSON). */
function formatWarning(message, args) {
  let index = 0
  return message.replace(/%C/g, () => JSON.stringify(args[index++]))
}

const options = parseArgs(process.argv.slice(2))
const profilesDir = join(options.home, 'profiles')
const profileDir = join(profilesDir, options.profile)
const profileManifest = join(profileDir, 'package.json')
const anchor = profileManifest

const failures = []
const note = message => process.stdout.write(`${message}\n`)

if (!existsSync(profileManifest)) {
  note(`result: FAILED — ${profileManifest} is missing`)
  process.exit(1)
}

const manifest = JSON.parse(readFileSync(profileManifest, 'utf8'))
const bundles = manifest.dsh?.profile?.bundles ?? []
note(`profile:  ${profileDir}`)
note(`bundles:  ${bundles.join(', ')}`)
note(`reload:   ${manifest.dsh?.profile?.patchReload ?? 'live (default)'}`)

const { applyEntryPatches } = await importFrom(
  createRequire(anchor).resolve('@deepseek-ai/cordis-plugin-include'),
  '@deepseek-ai/cordis-plugin-include',
)
const includeAnchor = createRequire(anchor).resolve('@deepseek-ai/cordis-plugin-include')
const yamlModule = await importFrom(includeAnchor, 'js-yaml')
const yaml = yamlModule.default ?? yamlModule
// Bundle patches use `!!js` for platform probes; its value is irrelevant to the
// composition we assert, so it is carried as an opaque marker.
const jsType = new yaml.Type('tag:yaml.org,2002:js', {
  kind: 'scalar',
  construct: data => ({ __js: String(data) }),
})
const schema = yaml.DEFAULT_SCHEMA.extend([jsType])

function loadPatchList(path) {
  const parsed = yaml.load(readFileSync(path, 'utf8'), { schema, filename: path })
  return Array.isArray(parsed) ? parsed : []
}

const layers = []
const unresolved = []
for (const name of bundles) {
  const dir = packageDirFrom(anchor, name)
  if (!dir) {
    unresolved.push(name)
    continue
  }
  const bundleManifest = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8'))
  const declared = bundleManifest.dsh?.bundle?.patch
  if (!declared) {
    unresolved.push(`${name} (no dsh.bundle.patch)`)
    continue
  }
  layers.push(loadPatchList(join(dir, declared)))
}
const userPatchPath = join(profileDir, 'cordis.patch.yml')
if (!existsSync(userPatchPath)) {
  failures.push(`${userPatchPath} is missing`)
} else {
  layers.push(loadPatchList(userPatchPath))
}
note(`layers:   ${layers.length} patch list(s)${unresolved.length > 0 ? `; not resolvable offline: ${unresolved.join(', ')}` : ''}`)

const warnings = []
const entries = applyEntryPatches([], layers.flat(), (message, ...args) => warnings.push(formatWarning(message, args)))
note(`entries:  ${entries.length} top-level after composition`)

const bridge = findEntry(entries, 'web-mcp')
const web = findEntry(entries, 'web')

if (!bridge) {
  failures.push('no entry with id "web-mcp" — the insert patch did not apply')
} else {
  note(`\nweb-mcp:\n  name:   ${bridge.name}`)
  for (const [key, value] of Object.entries(bridge.config ?? {})) note(`  ${key.padEnd(20)}${JSON.stringify(value)}`)
  if (bridge.name !== 'llm-dsh-web-mcp') failures.push(`web-mcp name is ${JSON.stringify(bridge.name)}, expected "llm-dsh-web-mcp"`)
  const config = bridge.config ?? {}
  if (typeof config.command !== 'string' || !existsSync(config.command)) {
    failures.push(`web-mcp config.command does not exist: ${JSON.stringify(config.command)}`)
  }
  if (!Array.isArray(config.args)) failures.push('web-mcp config.args must be an array')
  if (typeof config.cwd !== 'string' || !existsSync(config.cwd)) {
    failures.push(`web-mcp config.cwd does not exist: ${JSON.stringify(config.cwd)}`)
  }
  if (typeof config.providerId !== 'string') failures.push('web-mcp config.providerId must be a string')
}

const expectedProvider = bridge?.config?.providerId ?? 'llmmcp'
if (!web) {
  failures.push('no entry with id "web" — the ctx.web row is missing from the composed layers')
} else {
  note(`\nweb:\n  name:   ${web.name}\n  config: ${JSON.stringify(web.config ?? {})}`)
  const config = web.config ?? {}
  if (config.searchProvider !== expectedProvider) {
    failures.push(`web.config.searchProvider is ${JSON.stringify(config.searchProvider)}, expected ${JSON.stringify(expectedProvider)}`)
  }
  if (config.fetchProvider !== expectedProvider) {
    failures.push(`web.config.fetchProvider is ${JSON.stringify(config.fetchProvider)}, expected ${JSON.stringify(expectedProvider)}`)
  }
}

// The loader imports the row by bare package name from the profile directory.
try {
  const resolved = createRequire(anchor).resolve('llm-dsh-web-mcp')
  note(`\nresolve:  llm-dsh-web-mcp → ${resolved}`)
  const module = await import(pathToFileURL(resolved).href)
  if (typeof module.apply !== 'function') failures.push('the resolved plugin has no apply()')
  if (module.name !== 'web-mcp') failures.push(`plugin name is ${JSON.stringify(module.name)}, expected "web-mcp"`)
  if (!Array.isArray(module.inject) || !module.inject.includes('web')) failures.push('the plugin does not inject "web"')
} catch (error) {
  failures.push(`llm-dsh-web-mcp does not resolve from ${dirname(anchor)}: ${error.message}`)
}

note(`\nwarnings: ${warnings.length === 0 ? 'none' : ''}`)
for (const warning of warnings) note(`  - ${warning}`)
if (warnings.length > 0) {
  if (unresolved.length === 0) {
    failures.push(`${warnings.length} patch warning(s) — a patch matched nothing`)
  } else {
    // A bundle layer we could not read offline may define the missing rows, so
    // these are reported, not failed on. Re-run inside the packaged app (where
    // every layer resolves) before treating them as real.
    note(`  (not treated as failures: ${unresolved.length} bundle layer(s) could not be read offline)`)
  }
}

if (failures.length > 0) {
  note('\nresult: FAILED')
  for (const failure of failures) note(`  - ${failure}`)
  process.exit(1)
}
note('\nresult: OK')
