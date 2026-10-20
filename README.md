# LLMMcp

An MCP server that gives agents two tools over the network this machine actually
has:

- `web_search` — Google first (rendered with a local headless browser), with
  Bing / Google News fallbacks.
- `web_fetch` — HTTP(S) retrieval converted to markdown, text or raw HTML.
- `web_diagnose` — the proxy, TLS and browser state the server resolved.

Direct sockets are blocked here and every request has to leave through the
enterprise proxy, which also terminates TLS with an internal CA. LLMMcp owns that
path so agent tools no longer depend on a search-provider API key.

## Why this exists

Two failures motivated the project:

1. The harness built-in `web_fetch` cannot reach the internet from this host
   (`TypeError: fetch failed`): Node does not use the Windows proxy settings and
   does not trust the corporate interception CA.
2. The built-in `web_search` needs a provider API key (`DEEPSEEK_API_KEY`), which
   this deployment does not have.

LLMMcp closes both gaps:

- **Proxy resolution** in one place: `LLMMCP_PROXY`, then `HTTPS_PROXY` /
  `HTTP_PROXY` / `ALL_PROXY`, then the config file, then Windows Internet
  Settings, with `NO_PROXY` honoured. Proxy credentials are never logged.
- **Corporate TLS** through `truststore`, which reads the Windows certificate
  store, so interception CAs validate instead of failing.
- **Google without an API key**: Google serves a JavaScript-only shell to plain
  HTTP clients, so the Google engine renders the results page in local headless
  Chrome or Edge and parses the settled DOM. Google result anchors are opaque
  `/goto?url=...` tokens, and each one is resolved to its real target URL through
  the redirect's `Location` header.
- **Offline-testable parsers**: every engine is tested against captured HTML
  fixtures, so the suite runs without network access.

## Quick start

    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e ".[dev]"

Run the MCP server over stdio, which is what agent clients spawn:

    .\.venv\Scripts\python.exe -m llmmcp

Or use the one-shot CLI:

    .\.venv\Scripts\python.exe -m llmmcp search "ascend npu mfu"
    .\.venv\Scripts\python.exe -m llmmcp fetch https://example.com
    .\.venv\Scripts\python.exe -m llmmcp diagnose

## MCP tools

### web_search

Arguments: `query` (required), `max_results` (default 8), `engine` (default
`auto`; also `google`, `bing`, `gnews`), `lang`, `region`, `recency`
(`d`/`w`/`m`/`y`), `timeout` (default 25 s).

Returns a numbered text list plus `structuredContent` shaped as
`{query, engine, results: [{title, url, snippet, engine}], elapsed_ms, warnings}`.

### web_fetch

Arguments: `url` (required), `format` (`markdown` default, or `text`, `html`),
`max_chars` (default 100000, sets `truncated` when hit), `timeout` (default 30 s),
`include_links`.

Returns the content plus `structuredContent` shaped as
`{url, final_url, status, content_type, title, content, truncated, bytes_read, elapsed_ms}`.

### web_diagnose

Reports the resolved proxy label (never credentials), TLS mode, browser path,
engine availability and an optional live connectivity probe.

## Configuration

Environment variables win over the config file; the file is read from
`$LLMMCP_CONFIG` or `~/.llmmcp/config.json`.

| key | default | meaning |
|---|---|---|
| `LLMMCP_PROXY` | unset | explicit proxy URL, overrides everything |
| `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` | unset | standard proxy variables |
| `NO_PROXY` | unset | bypass list, comma separated |
| `LLMMCP_TLS` | `truststore` | `truststore`, `certifi` or `off` |
| `LLMMCP_TIMEOUT` | `30` | default request timeout, seconds |
| `LLMMCP_MAX_BYTES` | `5000000` | response body cap |
| `LLMMCP_ENGINES` | `google,bing,gnews` | fallback order for `engine=auto` |
| `LLMMCP_GOOGLE_HOST` | `www.google.com` | Google host |
| `LLMMCP_BROWSER` | auto-detected | Chrome or Edge executable used for rendering |
| `LLMMCP_LOG_LEVEL` | `INFO` | logging verbosity |

## Design

    src/llmmcp/
      settings.py   configuration and proxy resolution
      http.py       the only place that opens sockets
      browser.py    headless Chrome/Edge DOM rendering
      fetch.py      URL to markdown/text/html
      search/       one module per engine plus the fallback chain
      server.py     MCP tool definitions
      cli.py        one-shot commands for humans and smoke tests

The contracts that keep these layers independent live in `models.py`
(`SearchResult`, `SearchResponse`, `FetchResponse`, `LlmmcpError`) and
`search/base.py` (`SearchRequest`, `SearchContext`, `SearchEngine`). Engines never
import the server or the transport; they receive an `HttpClient` and a `render`
callable through `SearchContext`.

## Testing

    .\.venv\Scripts\python.exe -m pytest -q          # offline, fixture based
    .\.venv\Scripts\python.exe tools\mcp_smoke.py    # MCP stdio handshake

`tools/mcp_smoke.py` speaks raw newline-delimited JSON-RPC, so it verifies the
wire protocol rather than an SDK. Pass `--call web_fetch --args-file args.json`
to exercise a tool.

## Using it from DSH

`packages/dsh-web-mcp/` contains a small DSH plugin that registers this MCP
server's search and fetch as `ctx.web` providers, which makes the harness native
`web_search` and `web_fetch` tools run through LLMMcp. See that package's README
for the wiring and rollback steps.

## Limitations

- Google rendering costs a browser launch: a search takes roughly 10 to 15 s on
  this machine. Other engines answer in about a second.
- Google markup is unofficial and can change; the parser reports a clear
  `GOOGLE_BLOCKED` error instead of returning empty results.
- Google News results point at `news.google.com/rss/articles/...` redirect links.
- Only text-like bodies are converted (`text/html`, `text/plain`,
  `application/json`, `application/xhtml+xml`); PDFs and binaries are refused
  with an explicit `UNSUPPORTED_CONTENT_TYPE` error.

## Repository rules

See `AGENTS.md` for the working agreements used when developing this repository.
