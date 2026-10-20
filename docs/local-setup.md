# Local setup: what stays on your machine

The repository carries code, tests and generic documentation only. Anything that
identifies a machine, a company or an account stays outside version control and
is read from configuration at runtime or by tests.

## Rules

1. **No absolute paths in code, tests or docs.** Use `~`, `<repo>` or a
   configuration value instead of `D:\WORK\...` or `C:\Users\<name>\...`.
2. **No real proxy, gateway or intranet host names.** Examples use
   `proxy.example.com`; the real value comes from configuration.
3. **No account names or company e-mail addresses.** Commit with a personal
   address (a GitHub noreply address is ideal), not a corporate one.
4. **No profile patches or backups in the repository.** A
   `cordis.patch.yml` contains the local profile path and the interpreter path
   of your machine; it is ignored by `.gitignore` and documented by template.

## Where local values come from

`LLMMCP_*` environment variables win. When they are unset, the file named by
`$LLMMCP_CONFIG` (or `~/.llmmcp/config.json`) is read:

```json
{
  "test_proxy": "http://proxy.example.com:8080",
  "deploy_dir": "<your DSH profile deploy directory>"
}
```

The proxy for real requests is resolved by `src/llmmcp/settings.py` in this
order: `LLMMCP_PROXY`, then `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`, then the
config file, then the Windows Internet Settings. Nothing in that chain is
written to the repository, and the resolved proxy is only ever reported as
`scheme://host:port` with any credentials removed.

## Tests

The suite never needs a real proxy: `tests/local_env.py` resolves optional local
values, and every assertion falls back to a generic placeholder, so a fresh
clone passes offline. Set `LLMMCP_TEST_PROXY` if you want the browser command
tests to exercise your real proxy shape.

## Before publishing

Run the companion check:

```powershell
python tools/check_repo_hygiene.py
```

It fails on absolute user paths, corporate e-mail addresses, intranet host names
and committed profile patches, so a public push never carries local detail.
