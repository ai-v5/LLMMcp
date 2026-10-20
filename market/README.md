# DSH plugin market index

`index.json` is a [DSH plugin
market](https://github.com/deepseek-ai/deepseek-harness) index in **format 1**: one
JSON document that lists installable DSH bundles with their display metadata and
the exact source of each version. DSH validates it with
`@deepseek-ai/dsh-plugin-market-index` before it shows or installs anything.

This index publishes one plugin:

| Plugin | Source | What it does |
|---|---|---|
| `llm-dsh-web-mcp` | `git`, this repository, `packages/dsh-web-mcp` | Serves the harness `web_search` / `web_fetch` tools from the local LLMMcp MCP server and installs that runtime automatically |

## Adding it to DSH

Discover reads only the index sources a user configured:

1. Open **Settings → Plugins → Discover** (or the Discover tab) and add a source.
2. Paste one of these URLs:

   - Published (raw file on `main`):
     `https://raw.githubusercontent.com/ai-v5/LLMMcp/main/market/index.json`
   - Local test, from a repository checkout:

     ```
     python -m http.server 8765
     ```

     then `http://127.0.0.1:8765/market/index.json`.

3. The plugin appears as **LLMMcp web bridge**; installing it runs pnpm against
   `git+https://github.com/ai-v5/LLMMcp.git#<commit>&path:/packages/dsh-web-mcp`,
   pinned to the commit recorded in `versions[0].source.commit`.
4. On first load the plugin resolves its Python runtime: it reuses
   `config.command`, then `~/.llmmcp/config.json`, and otherwise creates
   `~/.llmmcp/venv` and installs the published wheel. No manual `pip install`
   step is required.

After installing, point the harness web tools at the bridge (`cordis.patch.yml`):

```yaml
- id: web
  config:
    searchProvider: llmmcp
    fetchProvider: llmmcp
```

## Publishing a new version

1. Tag the release and publish a wheel, for example `v0.1.2`.
2. Bump the plugin `version` in `packages/dsh-web-mcp/package.json` and update the
   index entry:

   ```jsonc
   "versions": [
     {
       "version": "0.1.2",
       "publishTimestamp": 1792536671,   // Unix seconds
       "source": { "type": "git", "url": "https://github.com/ai-v5/LLMMcp",
                   "commit": "<40-hex commit>", "path": "packages/dsh-web-mcp" }
     }
   ]
   ```

   Versions are ordered newest-first by DSH; keep every version you still want
   installable.
3. Validate before pushing:

   ```
   python tools/validate_market_index.py
   ```

   The repository also validates the document with DSH's own parser when
   developing the index (that code is not vendored here).

## Format notes

- `format` must be `1`; `title` is required and must contain `en`.
- Category, label and sort-key ids that a plugin references must be declared in
  `metadata`.
- A Git source needs a canonical `https` URL without `.git`, a lowercase 40-hex
  commit, and an optional relative `path` to the package directory.
- `publishTimestamp` is an integer count of Unix seconds, below `10^11`.
- The index is unsigned: what the installed package manifest says wins over the
  index's claim. Installing is a normal plugin-manager install with its own
  approval.
