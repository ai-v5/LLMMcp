#!/usr/bin/env node
/**
 * A stand-in MCP stdio server for `verify.mjs --mock`.
 *
 * It speaks newline-delimited JSON-RPC 2.0 and implements exactly what the
 * bridge uses — `initialize`, `notifications/initialized`, `tools/list`,
 * `tools/call` — plus three deliberately awkward cases the bridge has to
 * survive: an `isError` result, a slow tool (timeout), and an unknown tool.
 * Nothing here touches the network; it exists so the client half of the bridge
 * can be verified without the Python server.
 */

import { createInterface } from 'node:readline'

const PROTOCOL_VERSION = '2025-06-18'

const TOOLS = [
  {
    name: 'web_search',
    description: 'mock web search',
    inputSchema: {
      type: 'object',
      properties: { query: { type: 'string' }, max_results: { type: 'integer' } },
      required: ['query'],
    },
  },
  {
    name: 'web_fetch',
    description: 'mock web fetch',
    inputSchema: {
      type: 'object',
      properties: { url: { type: 'string' }, format: { type: 'string' }, max_chars: { type: 'integer' } },
      required: ['url'],
    },
  },
  {
    name: 'web_diagnose',
    description: 'mock diagnostics',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'web_slow',
    description: 'mock tool that outlives the default timeout',
    inputSchema: { type: 'object', properties: {} },
  },
]

const HTML = [
  '<!doctype html>',
  '<html lang="en">',
  '<head><meta charset="utf-8"><title>Example Domain</title></head>',
  '<body><h1>Example Domain</h1><p>This domain is for use in illustrative examples.</p>',
  '<p><a href="https://www.iana.org/domains/example">More information...</a></p></body>',
  '</html>',
].join('\n')

function text(content) {
  return { type: 'text', text: content }
}

function result(structured, pretty) {
  return { content: [text(pretty)], structuredContent: structured }
}

function searchPayload(args) {
  const count = Number.isInteger(args.max_results) ? Math.max(1, Math.min(args.max_results, 3)) : 3
  const results = [
    { title: 'Ascend NPU MFU explained', url: 'https://example.com/ascend-mfu', snippet: 'Mock result about MFU on Ascend NPUs.', engine: 'mock' },
    { title: 'Model FLOPs utilization measurement', url: 'https://example.com/mfu-method', snippet: 'How MFU is computed for large model training.', engine: 'mock' },
    { title: 'CANN performance tooling', url: 'https://example.com/cann-prof', snippet: 'Profiling tools for Ascend clusters.', engine: 'mock' },
  ].slice(0, count)
  return { query: String(args.query ?? ''), engine: 'mock', results, elapsed_ms: 7 }
}

function fetchPayload(args) {
  return {
    url: String(args.url ?? ''),
    final_url: String(args.url ?? ''),
    status: 200,
    content_type: 'text/html; charset=UTF-8',
    title: 'Example Domain',
    content: HTML,
    truncated: false,
    bytes_read: Buffer.byteLength(HTML, 'utf8'),
    elapsed_ms: 12,
  }
}

function handleCall(params) {
  const args = params && typeof params.arguments === 'object' && params.arguments !== null ? params.arguments : {}
  switch (params?.name) {
    case 'web_search': {
      if (args.query === '__error__') {
        return {
          isError: true,
          content: [
            text(JSON.stringify({ error: { code: 'SEARCH_BLOCKED', message: 'mock engine refused the query', hint: 'retry with a simpler query' } })),
          ],
        }
      }
      const payload = searchPayload(args)
      return result(payload, `mock search: ${payload.results.length} results`)
    }
    case 'web_fetch': {
      const payload = fetchPayload(args)
      return result(payload, payload.content)
    }
    case 'web_diagnose':
      return result({ proxy: 'none', tls: 'mock', browser: null }, 'mock diagnose')
    case 'web_slow':
      return null
    default:
      throw Object.assign(new Error(`Unknown tool: ${String(params?.name)}`), { rpcCode: -32602 })
  }
}

const write = (message) => process.stdout.write(`${JSON.stringify(message)}\n`)

function reply(id, value) {
  write({ jsonrpc: '2.0', id, result: value })
}

function replyError(id, code, message) {
  write({ jsonrpc: '2.0', id, error: { code, message } })
}

const pendingSlow = new Set()

function onMessage(message) {
  if (!message || typeof message !== 'object') return
  if (typeof message.method !== 'string') return
  const { id, method, params } = message
  if (id === undefined || id === null) {
    // notification: nothing to answer
    return
  }
  switch (method) {
    case 'initialize':
      reply(id, {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: 'fake-llmmcp', version: '0.0.0' },
        instructions: 'mock server',
      })
      return
    case 'tools/list':
      reply(id, { tools: TOOLS })
      return
    case 'tools/call': {
      let outcome
      try {
        outcome = handleCall(params)
      } catch (error) {
        replyError(id, error.rpcCode ?? -32603, error.message)
        return
      }
      if (outcome === null) {
        const timer = setTimeout(() => {
          pendingSlow.delete(id)
          reply(id, result({ slow: true }, 'slow tool finished'))
        }, 5000)
        pendingSlow.add(timer)
        return
      }
      reply(id, outcome)
      return
    }
    case 'ping':
      reply(id, {})
      return
    default:
      replyError(id, -32601, `Method not found: ${method}`)
  }
}

if (process.argv.includes('--crash-on-start')) {
  process.stderr.write('fake-llmmcp: asked to crash on start\n')
  process.exit(3)
}

const lines = createInterface({ input: process.stdin, crlfDelay: Infinity })
lines.on('line', (line) => {
  if (line.trim() === '') return
  let message
  try {
    message = JSON.parse(line)
  } catch {
    process.stderr.write('fake-llmmcp: ignoring a non-JSON line\n')
    return
  }
  onMessage(message)
})
lines.on('close', () => {
  for (const timer of pendingSlow) clearTimeout(timer)
  process.exit(0)
})
process.stderr.write('fake-llmmcp: stdio server ready\n')
