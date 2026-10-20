# AGENTS.md — LLMMcp

## Scope

Repository rules for LLMMcp: an MCP server that provides `web_search`, `web_fetch`
and `web_diagnose` over the corporate proxy.

## Architecture contracts

- `src/llmmcp/models.py` and `src/llmmcp/search/base.py` are frozen contracts.
  Changing a field means updating the server, every engine and the DSH bridge in
  the same change.
- `src/llmmcp/http.py` is the only place that opens sockets. Engines receive an
  `HttpClient` and an optional `render` callable through `SearchContext`; they
  must not build their own sessions or spawn browsers.
- `src/llmmcp/browser.py` is the only place that spawns a headless browser.
- The search layer must stay importable without the server (tests and CLI rely
  on it), so it may not import `llmmcp.server`.

## Environment facts

- This host has no direct egress. Requests leave through the corporate proxy
  resolved by `settings.py`; tests must not assume otherwise.
- Corporate TLS interception is handled by `truststore`; do not disable
  verification to make a test pass.
- Windows console is GBK: anything that prints to the console must reconfigure
  stdout to UTF-8 or escape non-ASCII output.
- Spawned child processes must never open a console window on Windows
  (`CREATE_NO_WINDOW`).

## Credentials

Proxy URLs may carry a live credential. Only `settings.redact_url()` output may
be printed, logged, stored in a report or committed. Never place a proxy
credential in fixtures, tests, docs or commit messages.

## Testing

- `python -m pytest -q` must pass offline; network-dependent checks live in
  scripts and are marked or kept out of the default suite.
- Engine parsers are covered by captured HTML fixtures under
  `tests/fixtures/search/`. Refresh a fixture only from a real response and keep
  it free of personal data.
- `tools/mcp_smoke.py` is the protocol-level check: it speaks raw JSON-RPC over
  stdio and must keep working without any MCP SDK.

## Development flow

- Work on a branch and deliver through a pull request; do not commit directly to
  `main`.
- One logical change per commit. Do not reformat unrelated files.
- The DSH bridge in `packages/dsh-web-mcp/` is optional to the Python server: it
  must never become a runtime dependency of `src/llmmcp`.
