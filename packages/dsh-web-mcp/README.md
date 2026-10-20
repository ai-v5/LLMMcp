# llm-dsh-web-mcp

让 DSH 原生的 `web_search` / `web_fetch` 工具**改由本地 LLMMcp MCP 服务提供**的桥接插件。

DSH 的 `ctx.web` 是一个 provider 接缝（`@deepseek-ai/dsh-web`）：`@deepseek-ai/dsh-tool-web` 只负责模型可见的工具，真正检索/抓取由注册进来的 provider 完成。本插件向 `ctx.web` 注册两个 provider（`searchProvider` / `fetchProvider` 都叫 `llmmcp`），内部通过 **stdio MCP** 调 LLMMcp 的 `web_search` / `web_fetch`。

```
web_search 工具 ─┐                        ┌─ LLMMcpSearchProvider ─┐
                 ├─ ctx.web (dsh-web) ────┤                        ├─ MCP stdio ─ python -m llmmcp
web_fetch  工具 ─┘                        └─ LLMMcpFetchProvider ──┘
```

## 目录

| 文件 | 作用 |
|---|---|
| `index.js` | cordis 插件（`name`/`inject`/`apply`）+ 最小 MCP stdio JSON-RPC 客户端 + 两个 provider |
| `scripts/verify.mjs` | 用假 ctx 直接驱动 provider，**真实拉起 MCP 子进程**（`--mock` 用内置假服务） |
| `scripts/verify-seam.mjs` | 挂载**真实** `@deepseek-ai/dsh-web` 服务，走 `ctx.web.search()` / `ctx.web.fetch()` |
| `scripts/check-profile.mjs` | 离线按 launcher 语义重建 profile 生效条目，校验补丁是否真的生效 |
| `scripts/fake-mcp-server.mjs` | `--mock` 用的假 stdio MCP 服务（含错误/超时/未知工具用例） |

## 装配（本机已就位）

**1) 让 profile 能按裸名解析插件**（`profiles/node_modules` 是各 profile 共享的解析回退目录）：

```powershell
New-Item -ItemType Junction `
  -Path   "C:\Users\h00660248\.dsh\profiles\node_modules\llm-dsh-web-mcp" `
  -Target "D:\WORK\AI-V5\LLMMcp\.dsh_worktrees\feat-initial\packages\dsh-web-mcp"
```

`desktop` profile 由 pnpm 管理（`nodeLinker: hoisted`），也可以改成声明式依赖
`"llm-dsh-web-mcp": "link:<绝对路径>"` 后 `pnpm install`；本机用 junction 是为了不触碰锁文件。

**2) 在 `%DSH_HOME%\profiles\desktop\cordis.patch.yml` 末尾追加**（`%DSH_HOME%` = `C:\Users\h00660248\.dsh`）：

```yaml
- insert:
    - id: web-mcp
      name: llm-dsh-web-mcp
      config:
        providerId: llmmcp
        command: 'D:\WORK\AI-V5\LLMMcp\.dsh_worktrees\feat-initial\.venv\Scripts\python.exe'
        args: ['-m', 'llmmcp']
        cwd: 'D:\WORK\AI-V5\LLMMcp\.dsh_worktrees\feat-initial'
        toolCallTimeoutMs: 60000
        searchTimeoutS: 55
        fetchTimeoutS: 40
        warmup: true

- id: web
  name: '@deepseek-ai/dsh-web'
  config:
    searchProvider: llmmcp
    fetchProvider: llmmcp
```

要点：
- `insert` 无 `id` 时**追加到根条目列表**；`id: web` 是 `@deepseek-ai/dsh-base` 里的 `ctx.web` 服务条目
  （原值 `searchProvider: deepseek-official` / `fetchProvider: http`）。
- 补丁语义是**整体替换该条目的 `config`**，而 `dsh-web` 的 config 只有这两个字段，所以不会丢配置。
- patch 里的 `name` 必须与目标条目一致，否则该补丁被跳过（日志有 `patch: name mismatch` 警告）。
- `desktop` profile 未写 `dsh.profile.patchReload` → 默认 `live`：改 `cordis.patch.yml` 会热重载，
  但**首次装配/装 junction 后请重启 DSH**，确保模块解析与 fiber 都重新走一遍。

## 配置项

| 字段 | 默认 | 说明 |
|---|---|---|
| `providerId` | `llmmcp` | 注册到 `ctx.web` 的 provider id；`searchProvider`/`fetchProvider` 要写它 |
| `command` | — （必填） | MCP 服务可执行文件；Windows 用 venv 里 `python.exe` 的**绝对路径** |
| `args` | `[]` | 例：`['-m','llmmcp']` |
| `cwd` | 进程 cwd | 子进程工作目录；建议指到 LLMMcp worktree 根 |
| `env` | `{}` | 追加环境变量（覆盖父环境）。父环境里 `*KEY*/*TOKEN*/*SECRET*/*PASSWORD*/*AUTH*` 形状的变量**不会**传给子进程；代理变量会正常传递 |
| `searchTool` | `web_search` | LLMMcp 搜索工具名 |
| `fetchTool` | `web_fetch` | LLMMcp 抓取工具名 |
| `toolCallTimeoutMs` | `60000` | 单次 `tools/call` 的传输层预算 |
| `searchTimeoutS` | 派生 `min(55, toolCallTimeoutMs/1000-5)` | 传给 `web_search` 的 `timeout`（秒）。**必须显式给**：LLMMcp 侧默认 25s，而 Google 首查要渲染浏览器 |
| `fetchTimeoutS` | 派生 `min(40, toolCallTimeoutMs/1000-5)` | 传给 `web_fetch` 的 `timeout`（秒） |
| `warmup` | `true` | 插件加载时就拉起子进程并 `tools/list`，避免首次工具调用等解释器启动 |
| `fetchFormat` | `html` | 传给 `web_fetch` 的 `format`；`dsh-tool-web` 自己把 HTML 转 markdown 并做长度控制 |
| `fetchMaxChars` | `400000` | 传给 `web_fetch` 的 `max_chars`（大于工具层 200k 输出上限，避免 provider 侧先截断） |
| `protocolVersion` | `2025-06-18` | `initialize` 请求的协议版本，属 legacy 握手版本（换行分隔 JSON） |
| `clientName` / `clientVersion` | `dsh-web-mcp` / `0.1.0` | 上报给 MCP 服务的客户端信息 |
| `stderrTailChars` | `4000` | 子进程 stderr 保留长度，只用于错误消息 |

**不导出 schemastery `Config`**：`@deepseek-ai/schemastery` 只能从 harness 依赖图解析到，
插件在 profile 里是 `link:` 进来的零依赖包；在模块顶层 import 它会因解析失败而整行插件加载失败。
配置在 `resolveOptions()` 里校验，字段错会抛出带字段名的错误。

## 验证

```powershell
cd D:\WORK\AI-V5\LLMMcp\.dsh_worktrees\feat-initial\packages\dsh-web-mcp

node scripts/verify.mjs --mock --checks   # 不需要 Python：错误/超时/取消/复用全链路
node scripts/verify.mjs                   # 真实 LLMMcp：search 条数+首条标题、fetch 状态+长度、耗时
node scripts/verify-seam.mjs              # 挂真实 ctx.web 服务：ctx.web.search()/fetch()
node scripts/check-profile.mjs            # 只读：检查 profile 补丁真的生效、插件可解析
```

预期（本机实测，2026-10-20）：

```
timeouts:   search=55s fetch=40s
search("ascend npu mfu", 5)   sources: 5   elapsed: ~11-13 s
fetch("https://example.com")  status: 200  body len: 577  elapsed: ~0.6 s
second fetch (connection reuse): ~0.1 s
result: OK
```

`check-profile.mjs` 在打包安装环境下会提示若干 bundle 层"offline 不可读"（它们在 `app.asar` 里），
这类 `patch: entry ... not found` 警告不算失败；`web-mcp` 与 `web` 两个条目必须出现且 config 正确。

## 回滚

```powershell
# 1) 恢复补丁（改坏了就用备份；备份名 cordis.patch.yml.bak-<时间>）
Copy-Item -LiteralPath "C:\Users\h00660248\.dsh\profiles\desktop\cordis.patch.yml.bak-20261020-155201" `
          -Destination "C:\Users\h00660248\.dsh\profiles\desktop\cordis.patch.yml" -Force

# 2) 卸载插件软链（可选）
Remove-Item -LiteralPath "C:\Users\h00660248\.dsh\profiles\node_modules\llm-dsh-web-mcp"

# 3) 重启 DSH
```

最小回滚 = 删掉 `cordis.patch.yml` 里 `web-mcp` 与 `id: web` 两个条目后重启：`ctx.web` 回到
`deepseek-official` / `http`，原生工具行为完全恢复（插件本身不写任何持久状态）。
也可以用环境变量临时兜底：`DSH_WEB_SEARCH_PROVIDER` / `DSH_WEB_FETCH_PROVIDER`（但 config 优先级更高，
两者同时存在时以 `cordis.patch.yml` 为准）。

## 故障排查

| 现象 | 处理 |
|---|---|
| `web_search` 首次 10~20 s | 正常：Google 结果页对普通 HTTP 客户端是 JS 门禁，LLMMcp 用 headless 浏览器渲染，耗时随机器负载变化 |
| `GOOGLE_RENDER_FAILED: headless browser timed out` | 先 `llmmcp diagnose`（`python -m llmmcp diagnose`）看浏览器/代理；再查是否有残留 `chrome/msedge` 进程；必要时把 `searchTimeoutS` 提到 55（已默认） |
| `SEARCH_ALL_FAILED` 且 bing 报相关性守卫 | 查询词太泛/太长，换更具体的查询；这是 LLMMcp 引擎链的行为，不是桥接问题 |
| 工具报 `configured web provider "llmmcp" is not registered` | 插件没加载成功：看 DSH 日志里 `web-mcp` 行；核对 `name: llm-dsh-web-mcp` 能否从 profile 目录 `require.resolve` |
| 工具报 `registered but unavailable` | `available()` 只做本地判断：`config.command` 不存在或不可执行 → 核对 venv python 绝对路径 |
| `LLMMcp warmup failed ...` | 子进程起不来：用同一 `command`/`args`/`cwd` 手跑 `python -m llmmcp` 看 stderr；错误文本会带 stderr 尾部 |
| 想确认实际用了哪个 provider | DSH 日志中 `ctx.web now has providers "llmmcp" ...`，或 `LMMcp MCP session ready (server=llmmcp ...)` |

**stdout 纪律**：插件从不写 stdout（stdout 属于宿主），子进程 stdout 只走 JSON-RPC；所有日志走
`ctx.logger`，stderr 尾巴在写进日志/错误前会把 `user:password@` 抹成 `***@`。

## 已知边界

- **非 2xx 语义**：内置 http provider 把非 2xx 当"结果"，LLMMcp 则抛 `FetchError`，所以桥接会**报错**
  而不是返回 `statusCode`；需要状态码时看错误文本里的 code/状态。
- **单连接串行**：一个子进程、一个 `tools/call` 队列；并发 `web_search` 会排队（DSH 工具本身也有 60s 预算）。
- **子进程回收**：`close()` 先关 stdin，250 ms 内未退出就 `taskkill /T /F`（venv 的 `python.exe` 可能是
  启动器，真正的解释器是它的子进程，只 kill 直接子进程会留孤儿）；宿主硬退出时用 `process.once('exit')`
  兜底同步 kill。
- **跨盘 junction**：插件本体在 `D:`，profile 在 `C:`；插件包零依赖，所以不需要 hoist 任何包到 profile。
